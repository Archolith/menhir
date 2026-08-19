"""LLM/embeddings adapter for scheduler-managed llama.cpp endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from menhir.config import MemorySettings
from menhir.infrastructure.providers import (
    ChatBackend,
    ProviderConfig,
    ProviderRuntimeDependencies,
    build_chat_backend,
)
_COMPRESS_MAX_RETRIES = 8
_COMPRESS_RETRY_BASE_DELAY = 2.0

_THINKING_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

logger = logging.getLogger(__name__)

_COMPRESS_SYSTEM_PROMPT = (
    "You are a memory compression assistant. "
    "Summarize the following memory content into a concise version that preserves "
    "all key facts, entities, and relationships. "
    "Output ONLY the summary, no preamble or explanation. "
    "Keep the summary under 200 characters when possible."
)

_REHYDRATE_SYSTEM_PROMPT = (
    "You update compressed memory summaries with new context. "
    "Preserve the important facts, entities, and relationships from the existing memory "
    "while incorporating the new context. "
    "Output ONLY the updated memory as a single concise statement."
)

_CONTRADICTION_SYSTEM_PROMPT = (
    "You are a memory conflict detector. "
    "Given two memory nodes, determine if they make genuinely incompatible claims "
    "about the same fact or entity. "
    "Different names for the same thing, related-but-distinct concepts, and "
    "complementary information are NOT contradictions. "
    "Reply with exactly one word: CONFLICT or CLEAR."
)

_IDENTITY_JUDGMENT_SYSTEM_PROMPT = (
    "You are an entity identity judge. "
    "Given two memory nodes, determine whether they refer to the same real-world entity. "
    "Names may differ (e.g., abbreviations, aliases, versions). "
    "Different entities with similar names are NOT the same entity. "
    "Reply with exactly one word: SAME or DIFFERENT."
)

# Shadow-mode context composition (Stage 1, .agent/plans/menhir-context-composition-production-
# integration.md): grounds classification in the REAL candidate fact-edges retrieved for THIS
# episode (fact text + both endpoint names), not an abstract hand-authored ontology and not a
# fixed known-triples list -- the lab's Phase 5 winning finding was that grounding in real,
# concrete existing labels (rather than an invented category ontology) closed the coverage gap.
# shadow_facet / shadow_state_family are explicitly NOT MemoryFacetSet fields -- they are labels
# synthesized live by this call, scoped only to the shadow trace, never persisted to the graph.
_SHADOW_GROUNDED_SYSTEM_PROMPT = """You are a routing component for a conversational memory
system, running in SHADOW mode (observe-only; nothing you say here changes what gets stored).

You are given a CURRENT MESSAGE and a list of REAL CANDIDATE FACTS that already exist in the
memory graph for entities the message might be about -- each has a fact_uuid, the fact text
itself, and the two entity names it connects.

Two tasks:
1. message_hypotheses: up to 2 ranked guesses at what topic/state the CURRENT MESSAGE is
   about, each as a short free-text (shadow_facet, shadow_state_family) label pair with a
   confidence 0.0-1.0. Ground these labels in the vocabulary the CANDIDATE FACTS themselves
   suggest -- do not invent a rigid taxonomy. If nothing plausibly matches, return an empty list.
2. candidate_labels: for EVERY candidate fact given, a (shadow_facet, shadow_state_family,
   shadow_scope) label grounded ONLY in that candidate's own fact text and endpoints -- describe
   what real-world topic/state that specific fact is about, independent of whether it matches the
   message.

Return JSON only:
{"message_hypotheses": [{"shadow_facet": "...", "shadow_state_family": "...", "confidence": 0.0}],
 "candidate_labels": [{"fact_uuid": "...", "shadow_facet": "...", "shadow_state_family": "...",
                        "shadow_scope": "..."}]}"""


def _shadow_grounded_user_prompt(
    episode_body: str,
    candidates: list[dict[str, str]],
) -> str:
    """Build the user prompt for classify_shadow_context. `candidates` entries carry
    fact_uuid, fact_text, source_name, target_name (already fetched by the caller)."""
    lines = "\n".join(
        f"- fact_uuid={c['fact_uuid']!r}: {c['source_name']} — {c['fact_text']} — {c['target_name']}"
        for c in candidates
    )
    return (
        f"CURRENT MESSAGE:\n{episode_body[:2000]}\n\n"
        f"REAL CANDIDATE FACTS:\n{lines or '(none retrieved)'}\n\n"
        'Return JSON only, matching the schema in the system prompt.'
    )


# Genuine-tie fallback (Stage 1's analogue of the lab's select_structured_then_llm fallback,
# .agent/plans/menhir-extraction-context-ablation-handoff.md Phase 5 item 4): consulted only
# when 2+ candidates survive the deterministic shadow-label filter. The lab's finding was that
# this path must be able to ABSTAIN under genuine irreducible ambiguity, not forced to guess --
# the prompt says so explicitly, mirroring that result.
_SHADOW_TIE_BREAK_SYSTEM_PROMPT = """You are resolving a genuine tie in a memory routing
shadow trace (observe-only; nothing you say here changes what gets stored). Multiple candidate
facts equally survived a deterministic filter for the CURRENT MESSAGE. If the message's own
wording clearly favors ONE candidate, return its fact_uuid. If nothing in the message
distinguishes them, DO NOT GUESS -- return null. Guessing under genuine ambiguity is worse than
abstaining.

Return JSON only: {"selected_fact_uuid": "..." or null}"""


def _shadow_tie_break_user_prompt(
    episode_body: str,
    tied_candidates: list[dict[str, str]],
) -> str:
    lines = "\n".join(
        f"- fact_uuid={c['fact_uuid']!r}: {c['source_name']} — {c['fact_text']} — {c['target_name']}"
        for c in tied_candidates
    )
    return (
        f"CURRENT MESSAGE:\n{episode_body[:2000]}\n\nTIED CANDIDATES:\n{lines}\n\n"
        'Return JSON only, e.g. {"selected_fact_uuid": "abc-123"} or {"selected_fact_uuid": null}'
    )


def _strip_thinking_tags(text: str) -> str:
    """Remove leaked reasoning blocks and normalize leftover whitespace."""
    stripped = _THINKING_TAG_RE.sub(" ", text)
    stripped = stripped.replace("<think>", " ").replace("</think>", " ")
    return " ".join(stripped.split())


@dataclass
class LLMAdapter:
    """Configuration holder for LLM + embedder endpoints."""

    base_url: str
    api_key: str
    chat_model: str
    embed_model: str
    backend: ChatBackend | None = None
    provider_kind: str | None = None  # ProviderKind as string (metadata; NOT the /no_think gate)
    dependencies: ProviderRuntimeDependencies = field(default_factory=ProviderRuntimeDependencies)

    def _wants_no_think(self) -> bool:
        """True when the chat model is a Qwen3-family model whose extended reasoning must be
        suppressed with the `/no_think` prompt token. Gated on the MODEL NAME, not the provider
        kind — ProviderKind has no 'qwen' member (local/openai/gemini/anthropic), and Qwen runs
        under kind 'local' here, so a kind-based gate would silently never fire."""
        return "qwen" in (self.chat_model or "").lower()

    @classmethod
    def from_settings(cls, settings: MemorySettings) -> "LLMAdapter":
        provider = ProviderConfig.for_chat(settings)
        return cls(
            base_url=provider.base_url,
            api_key=provider.api_key,
            chat_model=provider.chat_model,
            embed_model=provider.embed_model,
            backend=build_chat_backend(settings, provider=provider),
            provider_kind=str(provider.kind),  # Gate /no_think on provider kind
        )

    def chat_model_name(self) -> str:
        """Return chat model id for compatibility with tests."""
        return self.chat_model

    def embed_model_name(self) -> str:
        """Return embedding model id for compatibility with tests."""
        return self.embed_model

    async def _chat_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int = 256,
        temperature: float = 0.3,
        max_retries: int = _COMPRESS_MAX_RETRIES,
        retry_base_delay_s: float = _COMPRESS_RETRY_BASE_DELAY,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> str | None:
        """Run a single-text chat prompt with exponential backoff retry handling."""
        if not user_prompt.strip():
            return None

        backend = self.backend
        if backend is None:
            backend = build_chat_backend(MemorySettings.from_env(), dependencies=self.dependencies)
        last_error: Exception | None = None
        sleep = retry_sleep or self.dependencies.retry_sleep

        for attempt in range(max_retries + 1):
            try:
                raw = await backend.create_chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    operation=operation,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                result = _strip_thinking_tags(raw).strip()
                return result if result else None
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                logger.warning("LLM %s timed out; not retrying: %s", operation, exc)
                return None
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = min(retry_base_delay_s * (2 ** attempt), 60.0)
                    logger.warning(
                        "LLM %s attempt %d/%d failed, retrying in %.0fs: %s",
                        operation,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        exc,
                    )
                    await sleep(delay)

        logger.error(
            "LLM %s failed after %d attempts",
            operation,
            max_retries + 1,
            exc_info=last_error,
        )
        return None

    async def compress_content(self, content: str) -> str | None:
        """Ask the local LLM to summarize memory content for compression.

        Retries up to _COMPRESS_MAX_RETRIES times with a delay between attempts
        to handle transient LLM crashes. Returns the summary string, or None if
        all attempts fail.
        """
        return await self._chat_text(
            system_prompt=_COMPRESS_SYSTEM_PROMPT,
            user_prompt=content,
            operation="compression",
        )

    async def merge_content(self, existing_content: str, new_context: str) -> str | None:
        """Ask the local LLM to merge new context into a compressed memory."""
        if not existing_content.strip() or not new_context.strip():
            return None

        user_prompt = (
            f"Existing memory: {existing_content}\n"
            f"New context: {new_context}"
        )
        return await self._chat_text(
            system_prompt=_REHYDRATE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            operation="rehydration",
        )

    async def confirm_contradiction(
        self,
        *,
        name_a: str,
        content_a: str,
        name_b: str,
        content_b: str,
    ) -> bool | None:
        """Ask the LLM whether two nodes genuinely contradict each other.

        Returns True if contradiction confirmed, False if cleared as a false
        positive, or None if the LLM call failed and the group should be left
        in pending_llm_review for retry.
        """
        # /no_think suppresses Qwen3 extended reasoning — gate on the MODEL NAME
        # (see _wants_no_think: ProviderKind has no 'qwen' member; a kind gate never fires)
        thinking_token = ""
        if self._wants_no_think():
            thinking_token = "/no_think\n"

        user_prompt = (
            f"{thinking_token}"
            f"Node A — {name_a}\n{content_a or '(no content)'}\n\n"
            f"Node B — {name_b}\n{content_b or '(no content)'}"
        )
        raw = await self._chat_text(
            system_prompt=_CONTRADICTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            operation="contradiction_check",
            max_tokens=64,
            temperature=0.0,
            max_retries=0,  # fail fast — don't block MCP on a down server
        )
        if raw is None:
            return None
        token = raw.strip().upper().split()[0] if raw.strip() else ""
        if token == "CONFLICT":
            return True
        if token == "CLEAR":
            return False
        logger.warning("Unexpected contradiction check response: %r — treating as no confirmation", raw)
        return None

    async def confirm_same_entity(
        self,
        *,
        name_a: str,
        content_a: str,
        name_b: str,
        content_b: str,
    ) -> bool | None:
        """Ask the LLM whether two nodes refer to the same real-world entity.

        Used by the judge-gated merge pipeline (Part 2) to confirm identity before
        merging. Returns True if same entity confirmed, False if different entities,
        or None if the LLM call failed and the merge should be blocked (fail-safe).

        The prompt shows both nodes' names and content summaries, asks the neutral
        question without presupposing sameness, and expects a single-word response.
        """
        # /no_think suppresses Qwen3 extended reasoning — gate on the MODEL NAME
        # (see _wants_no_think: ProviderKind has no 'qwen' member; a kind gate never fires)
        thinking_token = ""
        if self._wants_no_think():
            thinking_token = "/no_think\n"

        user_prompt = (
            f"{thinking_token}"
            f"Node A — {name_a}\n{content_a or '(no content)'}\n\n"
            f"Node B — {name_b}\n{content_b or '(no content)'}"
        )
        raw = await self._chat_text(
            system_prompt=_IDENTITY_JUDGMENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            operation="identity_judgment",
            max_tokens=64,
            temperature=0.0,
            max_retries=0,  # fail fast — don't block MCP on a down server
        )
        if raw is None:
            return None
        token = raw.strip().upper().split()[0] if raw.strip() else ""
        if token == "SAME":
            return True
        if token == "DIFFERENT":
            return False
        logger.warning("Unexpected identity judgment response: %r — treating as no confirmation", raw)
        return None

    async def classify_shadow_context(
        self,
        episode_body: str,
        candidates: list[dict[str, str]],
    ) -> str | None:
        """Shadow-mode grounded classification (Stage 1, context-composition production-
        integration plan). Returns the raw LLM text (the caller parses/validates the JSON
        so it can distinguish a malformed response from a failed call), or None if the
        call itself failed. fail-fast (max_retries=0): shadow processing carries its own
        outer timeout budget and must never spend that budget on this call's retries.

        max_tokens scales with candidate count: the response schema requires one
        candidate_labels entry PER candidate (up to shadow_context_composition.py's
        _MAX_CANDIDATE_FACTS=30 cap), and a flat 800-token budget truncated the JSON
        mid-object on real graph data with >~10 candidates (confirmed via manual
        smoke test against menhir-lme-neo4j) -- surfacing as malformed_llm_response
        with a JSONDecodeError at the truncation point, not as an obviously-a-limit
        error.
        """
        return await self._chat_text(
            system_prompt=_SHADOW_GROUNDED_SYSTEM_PROMPT,
            user_prompt=_shadow_grounded_user_prompt(episode_body, candidates),
            operation="shadow_context_composition",
            max_tokens=max(800, 100 + len(candidates) * 80),
            temperature=0.0,
            max_retries=0,
        )

    async def break_shadow_tie(
        self,
        episode_body: str,
        tied_candidates: list[dict[str, str]],
    ) -> str | None:
        """Shadow-mode tie-break: consulted only when 2+ candidates survive the
        deterministic shadow-label filter. Returns raw LLM text (None on call failure);
        the caller parses/validates the JSON, including the null-selection case."""
        return await self._chat_text(
            system_prompt=_SHADOW_TIE_BREAK_SYSTEM_PROMPT,
            user_prompt=_shadow_tie_break_user_prompt(episode_body, tied_candidates),
            operation="shadow_context_composition_tie_break",
            max_tokens=128,
            temperature=0.0,
            max_retries=0,
        )

    async def repair_edge_facts(
        self,
        episode_content: str,
        edges: list[dict[str, str]],
    ) -> list[str | None]:
        """Use LLM to generate proper facts for edges with synthetic facts.

        Returns a list parallel to *edges* — each entry is the repaired fact
        string or None if the LLM failed to produce one.
        """
        if not edges:
            return []

        edge_lines = []
        for i, e in enumerate(edges, 1):
            edge_lines.append(
                f"{i}. {e['source']} → {e['target']} (type: {e['relation']})"
            )

        raw = await self._chat_text(
            system_prompt=(
                "You repair relationship facts in a knowledge graph. "
                "Given episode text and edge stubs, write a concise factual "
                "statement for each edge that captures the relationship described "
                "in the episode. Return one fact per line, numbered to match."
            ),
            user_prompt=(
                f"Episode:\n{episode_content[:2000]}\n\n"
                f"Edges to repair:\n" + "\n".join(edge_lines)
            ),
            operation="edge_fact_repair",
            max_tokens=512,
            temperature=0.2,
        )

        if not raw:
            return [None] * len(edges)

        # Parse numbered facts from LLM response.
        # _strip_thinking_tags may collapse newlines into spaces, so we split
        # on the numbered pattern rather than relying on line breaks.
        results: list[str | None] = [None] * len(edges)
        for match in re.finditer(r"(\d+)[.:\-)\s]+(.+?)(?=\s*\d+[.:\-)]|$)", raw.strip()):
            idx = int(match.group(1)) - 1
            fact = match.group(2).strip()
            if 0 <= idx < len(edges) and fact:
                results[idx] = fact
        return results
