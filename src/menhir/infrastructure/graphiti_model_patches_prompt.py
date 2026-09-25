"""Graphiti prompt-serialization, summarize, and NoneType.replace safety patches."""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


_GRAPHITI_PROMPT_MODULES = (
    "graphiti_core.prompts.prompt_helpers",
    "graphiti_core.prompts.dedupe_nodes",
    "graphiti_core.prompts.summarize_nodes",
    "graphiti_core.prompts.extract_nodes",
    "graphiti_core.prompts.extract_nodes_and_edges",
    "graphiti_core.prompts.eval",
    "graphiti_core.prompts.extract_edges",
)

#: Longest numeric list that can plausibly be real prompt content.  Anything longer
#: made entirely of numbers is an embedding vector, not something a model can read.
_MAX_PROMPT_NUMERIC_LIST_LEN = 64

def _prompt_json_default(value: Any) -> Any:
    """Serialize prompt data that may contain Neo4j temporal objects."""
    for attr_name in ("isoformat", "iso_format", "to_native"):
        method = getattr(value, attr_name, None)
        if callable(method):
            converted = method()
            if converted is value:
                break
            if isinstance(converted, (str, int, float, bool)) or converted is None:
                return converted
            return _prompt_json_default(converted)
    return str(value)


def _is_embedding_value(value: Any) -> bool:
    """Return True if *value* looks like an embedding vector rather than prompt content."""
    if not isinstance(value, (list, tuple)) or len(value) <= _MAX_PROMPT_NUMERIC_LIST_LEN:
        return False
    # Sampling the head is enough: embeddings are homogeneous by construction.
    return all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:8])


def _strip_embeddings(data: Any) -> Any:
    """Drop embedding vectors from prompt data before serialization.

    Graphiti hydrates ``:Entity`` attributes from ``properties(n)`` and pops only its
    own keys (uuid, name, group_id, name_embedding, summary, created_at, labels), so
    menhir-owned vectors survive into ``attributes``.  ``_resolve_with_llm`` then
    splats ``**candidate.attributes`` into the dedupe prompt, putting a 1536-float
    ``content_embedding`` (~31KB serialized) into the request for every candidate.
    At 15 candidates per extracted entity name that reached 1-3M tokens against a
    128K limit, so enrichment 400'd and the episode was left with zero entities and
    thus permanently unrecallable.

    A model cannot use raw floats; stripping them costs nothing and is not a
    dedup-quality tradeoff.  Both a key-name rule and a structural rule are applied
    so the next vector property added to a node does not reintroduce the bug.
    """
    if isinstance(data, dict):
        return {
            key: _strip_embeddings(value)
            for key, value in data.items()
            if not (
                (isinstance(key, str) and key.endswith("_embedding"))
                or _is_embedding_value(value)
            )
        }
    if isinstance(data, (list, tuple)):
        return [_strip_embeddings(item) for item in data]
    return data


def _safe_to_prompt_json(data: Any, ensure_ascii: bool = False, indent: int | None = None) -> str:
    """Serialize Graphiti prompt data with a compatibility fallback."""
    return json.dumps(
        _strip_embeddings(data),
        ensure_ascii=ensure_ascii,
        indent=indent,
        default=_prompt_json_default,
    )


def _patch_graphiti_prompt_json() -> None:
    """Install a JSON serializer compatible with Neo4j temporal values."""
    for module_name in _GRAPHITI_PROMPT_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            logger.debug("Skipping Graphiti prompt patch for missing module: %s", module_name)
            continue
        setattr(module, "to_prompt_json", _safe_to_prompt_json)


# ---------------------------------------------------------------------------
# Graphiti summarize-nodes patch
# ---------------------------------------------------------------------------


def _patch_graphiti_summarize() -> None:
    """Patch Graphiti's summarize_nodes prompts to produce structured key:value summaries.

    Default graphiti summaries are verbose prose (~250 chars). We replace them with
    a tighter key:value format (e.g. 'project:cth.mcp.memory | stack:Neo4j | status:active')
    that is more token-efficient when injected as recall context.
    """
    try:
        import graphiti_core.prompts.summarize_nodes as _sn_module
        from graphiti_core.prompts.models import Message

        def _summarize_context(context: dict[str, Any]) -> list[Message]:
            return [
                Message(
                    role="system",
                    content=(
                        "You are a concise knowledge-graph assistant. "
                        "Output structured key:value facts only. No prose, no explanation."
                    ),
                ),
                Message(
                    role="user",
                    content=f"""
Summarize the ENTITY using ONLY facts from the MESSAGES.
Format: key:value pairs separated by ' | '. Max 150 characters total.
Focus on: what it is, its role/status, key attributes. Omit filler words.

Good example: "project:cth.mcp.memory | stack:Neo4j+SQLite | status:M4 active | role:memory graph"
Bad example: "The cth.mcp.memory system is a project that uses Neo4j. It is currently in M4 phase."

<MESSAGES>
{context.get('previous_episodes', '')}
{context.get('episode_content', '')}
</MESSAGES>

<ENTITY>{context.get('node_name', '')}</ENTITY>
<ENTITY CONTEXT>{context.get('node_summary', '')}</ENTITY CONTEXT>
""",
                ),
            ]

        def _summarize_pair(context: dict[str, Any]) -> list[Message]:
            return [
                Message(
                    role="system",
                    content=(
                        "You are a concise knowledge-graph assistant. "
                        "Merge two structured summaries into one. Output key:value pairs only."
                    ),
                ),
                Message(
                    role="user",
                    content=f"""
Merge these two summaries into one structured key:value summary.
Format: key:value pairs separated by ' | '. Max 150 characters total.
Keep the most current/specific values. Drop duplicates.

Summaries:
{context.get('node_summaries', '')}
""",
                ),
            ]

        _sn_module.versions["summarize_context"] = _summarize_context  # type: ignore[assignment]
        _sn_module.versions["summarize_pair"] = _summarize_pair  # type: ignore[assignment]
        logger.debug("Graphiti summarize_nodes patched (structured key:value format)")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti summarize_nodes: %s", exc)


# ---------------------------------------------------------------------------
# Graphiti NoneType.replace safety patch
# ---------------------------------------------------------------------------


def _patch_graphiti_none_replace() -> None:
    """Guard Graphiti's .replace() calls that crash on None field values.

    Graphiti calls ``self.fact.replace('\\n', ' ')`` on EntityEdge and
    ``self.name.replace('\\n', ' ')`` on EntityNode/CommunityNode during
    embedding generation.  When the LLM returns null for these fields,
    the call crashes with ``'NoneType' object has no attribute 'replace'``.

    This patch wraps the three ``generate_*_embedding`` methods to coerce
    None values to empty strings before the original method runs.
    """
    patched = 0
    try:
        from graphiti_core.edges import EntityEdge

        if not getattr(EntityEdge, "_menhir_none_replace_patched", False):
            _orig_edge_embed = EntityEdge.generate_embedding

            async def _safe_edge_embed(self: Any, embedder: Any) -> None:
                if self.fact is None:
                    self.fact = ""
                await _orig_edge_embed(self, embedder)

            EntityEdge.generate_embedding = _safe_edge_embed  # type: ignore[assignment]
            EntityEdge._menhir_none_replace_patched = True  # type: ignore[attr-defined]
            patched += 1
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch EntityEdge.generate_embedding: %s", exc)

    try:
        from graphiti_core.nodes import CommunityNode, EntityNode

        if not getattr(EntityNode, "_menhir_none_replace_patched", False):
            _orig_entity_embed = EntityNode.generate_name_embedding

            async def _safe_entity_embed(self: Any, embedder: Any) -> None:
                if self.name is None:
                    self.name = ""
                await _orig_entity_embed(self, embedder)

            EntityNode.generate_name_embedding = _safe_entity_embed  # type: ignore[assignment]
            EntityNode._menhir_none_replace_patched = True  # type: ignore[attr-defined]
            patched += 1

        if not getattr(CommunityNode, "_menhir_none_replace_patched", False):
            _orig_community_embed = CommunityNode.generate_name_embedding

            async def _safe_community_embed(self: Any, embedder: Any) -> None:
                if self.name is None:
                    self.name = ""
                await _orig_community_embed(self, embedder)

            CommunityNode.generate_name_embedding = _safe_community_embed  # type: ignore[assignment]
            CommunityNode._menhir_none_replace_patched = True  # type: ignore[attr-defined]
            patched += 1
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Node.generate_name_embedding: %s", exc)

    logger.debug("Graphiti NoneType.replace safety patch applied (%d/3 methods)", patched)
