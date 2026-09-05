"""Graphiti prompt, model-normalization, and deduplication compatibility patches."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import importlib
import importlib.metadata
import json
import logging
from time import perf_counter
from typing import Any

from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
    check_graphiti_version,
)
from menhir.infrastructure.graphiti_extraction_patches import (
    _combined_extraction_cache,
    _extract_edges_from_combined_cache,
    get_extraction_receipt,
)
from menhir.infrastructure.graphiti_llm_patches import GraphitiRequestTooLargeError

logger = logging.getLogger(__name__)

# Version guard - run once at import, matching the other patch modules (CF-87).
check_graphiti_version()

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


#: Cap on distinct record keys retained per log-once set. Both sets are keyed by entity ``uuid``,
#: so their cardinality is bounded only by the malformed-entity population -- on a large corrupt
#: store they grow without bound, and they grow inside a patch that runs once per record of every
#: node search. Kept as ``set`` (not an insertion-ordered dict) because these objects are imported
#: BY REFERENCE elsewhere and the tests use the set API; eviction therefore mutates in place and
#: never rebinds the global.
_MALFORMED_LOG_KEYS_MAX = 512

_MALFORMED_ENTITY_GROUP_IDS_LOGGED: set[str] = set()
_MALFORMED_ENTITY_DATES_LOGGED: set[str] = set()


def _first_time_seen(seen: set[str], key: str) -> bool:
    """True the first time ``key`` is offered; evicts an arbitrary key at capacity.

    The victim is arbitrary rather than oldest because a ``set`` has no insertion order. That is
    acceptable here: the worst consequence of evicting the wrong key is one extra ERROR line for a
    record already reported, which is strictly better than unbounded growth.
    """
    if key in seen:
        return False
    if len(seen) >= _MALFORMED_LOG_KEYS_MAX:
        seen.pop()
    seen.add(key)
    return True


def _patch_graphiti_entity_record_group_id() -> None:
    """Keep a malformed stored entity from aborting an entire Graphiti search.

    Graphiti's Neo4j record adapter calls ``group_id.replace(...)`` without
    tolerating legacy records whose ``group_id`` property is null. Infer the
    canonical group from Menhir's namespace convention (default -> ``""``;
    named namespace -> that name), log the corrupt record once per process, and
    pass a defensive copy to Graphiti because its adapter mutates the record.
    """
    try:
        import graphiti_core.nodes as nodes_module
        import graphiti_core.search.search_utils as search_utils_module

        current = search_utils_module.get_entity_node_from_record
        if getattr(current, "_menhir_group_id_patched", False):
            return

        original = nodes_module.get_entity_node_from_record

        def _safe_entity_node_from_record(record: Any, provider: Any) -> Any:
            copied = dict(record)
            attributes = copied.get("attributes")
            copied["attributes"] = dict(attributes) if isinstance(attributes, dict) else attributes
            labels = copied.get("labels")
            copied["labels"] = list(labels) if labels is not None else []

            if copied.get("group_id") is None:
                namespace = (
                    copied["attributes"].get("namespace")
                    if isinstance(copied.get("attributes"), dict)
                    else None
                )
                inferred_group_id = "" if namespace in (None, "", "default") else str(namespace)
                copied["group_id"] = inferred_group_id
                record_key = str(copied.get("uuid") or f"{copied.get('name')}:{namespace}")
                if _first_time_seen(_MALFORMED_ENTITY_GROUP_IDS_LOGGED, record_key):
                    logger.error(
                        "Graphiti search encountered Entity with NULL group_id; "
                        "search continued with inferred_group_id=%r uuid=%r name=%r namespace=%r",
                        inferred_group_id,
                        copied.get("uuid"),
                        copied.get("name"),
                        namespace,
                    )

            created_at = copied.get("created_at")
            if isinstance(created_at, str) and created_at.endswith("Z[UTC]"):
                copied["created_at"] = created_at.removesuffix("Z[UTC]") + "+00:00"
                record_key = str(copied.get("uuid") or f"{copied.get('name')}:{created_at}")
                if _first_time_seen(_MALFORMED_ENTITY_DATES_LOGGED, record_key):
                    logger.error(
                        "Graphiti search encountered Entity with non-ISO created_at; "
                        "search continued with normalized timestamp uuid=%r name=%r created_at=%r",
                        copied.get("uuid"),
                        copied.get("name"),
                        created_at,
                    )

            return original(copied, provider)

        _safe_entity_node_from_record._menhir_group_id_patched = True  # type: ignore[attr-defined]
        nodes_module.get_entity_node_from_record = _safe_entity_node_from_record
        # search_utils imports the function directly, so patch its bound symbol too.
        search_utils_module.get_entity_node_from_record = _safe_entity_node_from_record
        logger.debug("Graphiti Entity record NULL group_id safety patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti Entity record group_id handling: %s", exc)


def _patch_graphiti_node_summary_none() -> None:
    """Coerce ``EntityNode(summary=None)`` to ``summary=''`` at construction.

    The ``summary`` field is typed ``str`` with ``default_factory=str`` (so a
    *missing* summary becomes ''), but when the extraction LLM explicitly returns
    ``null`` Graphiti builds ``EntityNode(summary=None)`` and Pydantic rejects it:
    ``1 validation error for EntityNode / summary / Input should be a valid string
    [input_value=None]`` — which fails the whole episode enrichment.  This wraps
    ``__init__`` to drop an explicit ``None`` summary so the field default applies.
    Idempotent (guards re-application).
    """
    try:
        from graphiti_core.nodes import EntityNode

        if getattr(EntityNode, "_yawn_summary_patched", False):
            return
        _orig_init = EntityNode.__init__

        def _safe_init(self: Any, **data: Any) -> None:
            if data.get("summary", "") is None:
                data["summary"] = ""
            _orig_init(self, **data)

        EntityNode.__init__ = _safe_init  # type: ignore[assignment]
        EntityNode._yawn_summary_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti EntityNode summary None-coercion patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch EntityNode summary None-coercion: %s", exc)


# EntityEdge fields backed by a default (factory): drop an explicit None so the
# default applies. ``uuid`` -> uuid4; ``episodes`` -> []. Coercing these to '' would
# be wrong (an empty uuid is a broken identity; episodes must be a list).
_EDGE_DROP_IF_NONE = ("uuid", "episodes")
# Required-str EntityEdge fields with no default: coerce an explicit None to ''.
_EDGE_REQUIRED_STR_FIELDS = ("group_id", "name", "fact", "source_node_uuid", "target_node_uuid")


def _patch_graphiti_edge_none_fields() -> None:
    """Coerce ``EntityEdge(...=None)`` fields at construction.

    Symmetric to ``_patch_graphiti_node_summary_none``. During dedupe/resolve the
    extraction LLM can return a fully-degenerate edge, and Graphiti builds
    ``EntityEdge(uuid=None, group_id=None, name=None, fact=None, episodes=None, ...)``
    — every one of those is required (str, or a list), so Pydantic raises ``N
    validation errors for EntityEdge`` and the whole episode's enrichment fails
    (observed recurring, incl. 2026-07-11). This wraps ``__init__`` to drop an
    explicit ``None`` for default-backed fields (uuid -> uuid4, episodes -> []) and
    coerce the required-str fields to '' so the episode's real nodes/edges still
    persist. Idempotent (guards re-application).
    """
    try:
        from graphiti_core.edges import EntityEdge

        if getattr(EntityEdge, "_yawn_edge_patched", False):
            return
        _orig_init = EntityEdge.__init__

        def _safe_init(self: Any, **data: Any) -> None:
            for key in _EDGE_DROP_IF_NONE:
                if data.get(key, "") is None:
                    data.pop(key, None)  # let the field's default_factory run
            for key in _EDGE_REQUIRED_STR_FIELDS:
                if data.get(key, "") is None:
                    data[key] = ""
            _orig_init(self, **data)

        EntityEdge.__init__ = _safe_init  # type: ignore[assignment]
        EntityEdge._yawn_edge_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti EntityEdge None-field coercion patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch EntityEdge None-field coercion: %s", exc)


# ---------------------------------------------------------------------------
# Graphiti entity-extraction patch
# ---------------------------------------------------------------------------


def _patch_graphiti_entity_extraction() -> None:
    """Patch Graphiti's entity extraction to tolerate 'entity_name' from Qwen3.

    Qwen3 sometimes returns ``entity_name`` instead of ``name`` in structured
    extraction responses, causing Pydantic validation failures in
    ``ExtractedEntities(**llm_response)``.  We replace the classes in both the
    prompts module (source) and the node_operations module (call site) so that
    either field name is accepted.
    """
    try:
        import graphiti_core.prompts.extract_nodes as _en_module
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from pydantic import BaseModel, Field, model_validator

        class PatchedExtractedEntity(BaseModel):
            name: str = Field(..., description="Name of the extracted entity")
            entity_type_id: int = Field(
                description="ID of the classified entity type. "
                "Must be one of the provided entity_type_id integers.",
            )
            # Graphiti 0.29 (multi-episode batching): _create_entity_nodes reads this
            # unconditionally (extracted_entity.episode_indices), so it must exist even
            # though Menhir's single-episode extraction path never populates it itself.
            # Mirrors graphiti_core.prompts.extract_nodes.ExtractedEntity's own default.
            episode_indices: list[int] = Field(
                default_factory=list,
                description="List of episode numbers (0-indexed) this entity was "
                "extracted from. When processing a single episode, this should be [0].",
            )

            @model_validator(mode="before")
            @classmethod
            def _remap_entity_fields(cls, data: Any) -> Any:
                if isinstance(data, dict):
                    data = dict(data)
                    # Handle degenerate {name: type_id} single-pair dicts
                    if len(data) == 1:
                        key, val = next(iter(data.items()))
                        if isinstance(val, int) and key not in ("name", "entity_type_id"):
                            return {"name": key, "entity_type_id": val}
                    if "name" not in data:
                        if "entity_name" in data:
                            data["name"] = data.pop("entity_name")
                        elif "entity" in data:
                            data["name"] = data.pop("entity")
                        else:
                            # LLM typo variants of the name key: 'name-', 'name_',
                            # 'Name ', etc. Normalize and adopt the first that matches.
                            for _k in list(data):
                                if (
                                    str(_k).strip().lower().rstrip("-_ ") == "name"
                                    and isinstance(data[_k], str)
                                ):
                                    data["name"] = data.pop(_k)
                                    break
                    if "entity_type_id" not in data:
                        if "type_id" in data:
                            data["entity_type_id"] = data.pop("type_id")
                        elif "type" in data and isinstance(data["type"], int):
                            data["entity_type_id"] = data.pop("type")
                        elif "type_name" in data:
                            data.pop("type_name")
                            data["entity_type_id"] = 0
                        elif "entity_type" in data:
                            val = data.pop("entity_type")
                            data["entity_type_id"] = val if isinstance(val, int) else 0
                        elif "entity" in data:
                            # Model returned {name: "foo", entity: "0"} — entity is the
                            # type ID in the wrong field (name was already set above).
                            val = data.pop("entity")
                            try:
                                data["entity_type_id"] = int(val)
                            except (TypeError, ValueError):
                                data["entity_type_id"] = 0
                        else:
                            # No type information at all — default to 0 (unclassified).
                            data["entity_type_id"] = 0
                return data

        class PatchedExtractedEntities(BaseModel):
            extracted_entities: list[PatchedExtractedEntity] = Field(
                ..., description="List of extracted entities"
            )

        _en_module.ExtractedEntity = PatchedExtractedEntity  # type: ignore[assignment]
        _en_module.ExtractedEntities = PatchedExtractedEntities  # type: ignore[assignment]
        _no_module.ExtractedEntity = PatchedExtractedEntity  # type: ignore[assignment]
        _no_module.ExtractedEntities = PatchedExtractedEntities  # type: ignore[assignment]
        logger.debug("Graphiti entity extraction patched (entity_name -> name)")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti entity extraction: %s", exc)


def _patch_graphiti_dedupe_resolutions() -> None:
    """Tolerate degenerate node-dedupe resolutions from the extraction LLM.

    Graphiti builds ``NodeResolutions(**llm_response)`` whose items (``NodeDuplicate``,
    Graphiti >=0.29 shape) require ``id:int``, ``name:str`` and
    ``duplicate_candidate_id:int`` (``-1`` means no duplicate — Graphiti's downstream
    resolver treats a negative candidate id as "no duplicate"). The LLM sometimes emits a
    degenerate entry such as ``{'': ''}`` (no id/name), which fails Pydantic at
    construction — *before* Graphiti's own downstream logic (which already skips
    out-of-range/missing ids) can run — and kills the whole episode's enrichment
    (observed recurring, incl. 2026-07-11).

    This wraps ``NodeResolutions`` with a before-validator that drops entries lacking a
    usable integer ``id`` (an id-less resolution is meaningless — id selects which
    entity is being resolved) and defaults a missing/None ``name`` to '' and a
    missing/None/non-integer ``duplicate_candidate_id`` to ``-1`` (fail-safe: "no
    duplicate" rather than guessing an arbitrary existing entity). Valid resolutions
    still apply; the rest are handled by Graphiti's existing "did not return resolutions
    for IDs" path.

    Single-user deployment: no compatibility shim for the pre-0.29
    ``duplicate_name:str`` shape — the dependency pin (``graphiti-core>=0.29.2,<0.30``)
    is the only supported version, so this patch targets the current shape only.
    """
    try:
        import graphiti_core.prompts.dedupe_nodes as _dn_module
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from pydantic import BaseModel, Field, model_validator

        _NodeDuplicate = _dn_module.NodeDuplicate

        class PatchedNodeResolutions(BaseModel):
            entity_resolutions: list[_NodeDuplicate] = Field(default_factory=list)

            @model_validator(mode="before")
            @classmethod
            def _drop_degenerate(cls, data: Any) -> Any:
                if not isinstance(data, dict):
                    return data
                data = dict(data)
                cleaned: list[dict[str, Any]] = []
                for item in data.get("entity_resolutions") or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        int(item.get("id"))  # id must be integer-coercible or the entry is unusable
                    except (TypeError, ValueError):
                        continue
                    item = dict(item)
                    if item.get("name") is None or "name" not in item:
                        item["name"] = ""
                    try:
                        item["duplicate_candidate_id"] = int(item.get("duplicate_candidate_id"))
                    except (TypeError, ValueError):
                        item["duplicate_candidate_id"] = -1  # fail-safe: "no duplicate"
                    cleaned.append(item)
                data["entity_resolutions"] = cleaned
                return data

        _dn_module.NodeResolutions = PatchedNodeResolutions  # type: ignore[assignment]
        _no_module.NodeResolutions = PatchedNodeResolutions  # type: ignore[assignment]
        logger.debug("Graphiti NodeResolutions degenerate-entry patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti NodeResolutions: %s", exc)


# ---------------------------------------------------------------------------
# Graphiti dedup prompt anti-conflation patch
# ---------------------------------------------------------------------------

# Additional example injected into the dedup prompt to prevent the LLM from
# merging a descriptive/relative location ("the suburbs", "downtown") into a
# specific named city.  The stock prompt covers same-name-different-thing
# ("Java" programming vs island) but has no example for
# different-name-related-location, which is the exact failure mode:
# gpt-4o-mini merges "the suburbs" into "Chicago" because both are locations
# mentioned near Rachel, even though the prompt says "NEVER mark entities as
# duplicates if they are related but distinct."

_DEDUP_ANTI_CONFLATION_EXAMPLE = """\

ENTITY: "the suburbs"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Chicago", "entity_types": ["Location"], "summary": "A city where someone lives"}}]
Result: duplicate_candidate_id = -1 (a relative or descriptive location like "the suburbs", "downtown", "the countryside" is NEVER the same real-world object as a specific named city, even if a person moved from one to the other or they are geographically related)
"""


def _patch_graphiti_dedup_prompt() -> None:
    """Inject an anti-conflation example into the node deduplication prompt.

    gpt-4o-mini (the extraction model) merges "the suburbs" into "Chicago"
    because both are location entities associated with Rachel.  The stock
    prompt says "NEVER mark entities as duplicates if they are related but
    distinct" — but the only examples cover same-name-different-thing cases.
    This patch wraps the ``node`` and ``nodes`` prompt functions to append
    an explicit example showing that a descriptive location must never be
    merged with a named city.  The wrapper is idempotent (guards
    re-application via a flag on the module).
    """
    try:
        import graphiti_core.prompts.dedupe_nodes as _dn_module

        if getattr(_dn_module, "_menhir_anti_conflation_patched", False):
            return

        _original_node = _dn_module.versions["node"]
        _original_nodes = _dn_module.versions["nodes"]

        def _patched_node(context: dict) -> list:
            messages = _original_node(context)
            for msg in messages:
                if msg.role == "user" and "</EXAMPLE>" in msg.content:
                    msg.content = msg.content.replace(
                        "</EXAMPLE>",
                        _DEDUP_ANTI_CONFLATION_EXAMPLE + "</EXAMPLE>",
                    )
            return messages

        def _patched_nodes(context: dict) -> list:
            messages = _original_nodes(context)
            for msg in messages:
                if msg.role == "user" and "</EXAMPLE>" in msg.content:
                    msg.content = msg.content.replace(
                        "</EXAMPLE>",
                        _DEDUP_ANTI_CONFLATION_EXAMPLE + "</EXAMPLE>",
                    )
            return messages

        _dn_module.node = _patched_node  # type: ignore[assignment]
        _dn_module.nodes = _patched_nodes  # type: ignore[assignment]
        _dn_module.versions["node"] = _patched_node
        _dn_module.versions["nodes"] = _patched_nodes
        _dn_module._menhir_anti_conflation_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti dedup prompt anti-conflation patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti dedup prompt: %s", exc)


# ---------------------------------------------------------------------------
# Post-LLM identity gate for dedup decisions
# ---------------------------------------------------------------------------

# After the LLM returns its dedup decisions, this gate validates each merge
# by checking for positive identity evidence between the extracted entity name
# and the target entity name.  If no positive evidence exists, the merge is
# overridden to "new entity" (duplicate_candidate_id = -1).
#
# Positive evidence (any one is sufficient):
#   - Exact name match (case-insensitive)
#   - One name is a substring of the other (≥3 chars)
#   - Acronym match (e.g. IBM ↔ International Business Machines)
#   - Shared token overlap ≥50% (Jaccard on lowered word tokens)
#
# This is the final safety net: if temperature=0 and the prompt patch both
# fail to prevent an incorrect merge, this gate catches it.  Every override
# is logged for analysis.
#
# See: Trial 10 FAIL_B_DEDUP_MERGED — "the suburbs" merged into "Chicago"
# has zero positive identity evidence under all four criteria.

_identity_gate_logger = logging.getLogger("menhir.dedup_identity_gate")


def _has_positive_identity_evidence(extracted_name: str, candidate_name: str) -> bool:
    """Return True if there is positive evidence that two names refer to the same entity.

    Conservative: returns True on any plausible match signal so legitimate merges
    (Bob→Robert, NYC→New York City, IBM→International Business Machines) are not blocked.
    Returns False only when there is genuinely no lexical relationship.
    """
    a = extracted_name.strip().lower()
    b = candidate_name.strip().lower()

    if not a or not b:
        return False

    # 1. Exact match
    if a == b:
        return True

    # 2. Substring (either direction, min 3 chars to avoid trivial matches like "a")
    if len(a) >= 3 and a in b:
        return True
    if len(b) >= 3 and b in a:
        return True

    # 3. Acronym: check if one name's initials match the other
    a_tokens = a.split()
    b_tokens = b.split()
    if len(a_tokens) == 1 and len(b_tokens) > 1:
        # a might be an acronym of b
        acronym = "".join(t[0] for t in b_tokens if t)
        if a.replace(".", "") == acronym:
            return True
    if len(b_tokens) == 1 and len(a_tokens) > 1:
        # b might be an acronym of a
        acronym = "".join(t[0] for t in a_tokens if t)
        if b.replace(".", "") == acronym:
            return True

    # 4. Token overlap (Jaccard ≥ 0.5)
    a_set = set(a_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    b_set = set(b_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    if a_set and b_set:
        intersection = a_set & b_set
        union = a_set | b_set
        if len(intersection) / len(union) >= 0.5:
            return True

    return False


def _edge_facts_mention(entity_name: str, cached_edges: list[Any] | None) -> set[str]:
    """Return the set of fact texts from cached edges that mention ``entity_name``."""
    if not cached_edges or not entity_name:
        return set()
    name_lower = entity_name.strip().lower()
    if len(name_lower) < 3:
        return set()
    facts: set[str] = set()
    for edge in cached_edges:
        fact = ""
        if hasattr(edge, "fact"):
            fact = edge.fact or ""
        elif isinstance(edge, dict):
            fact = edge.get("fact", "")
        if name_lower in fact.lower():
            facts.add(fact)
    return facts


def _patch_graphiti_dedup_identity_gate() -> None:
    """Wrap the LLM dedup resolver to veto merges lacking positive identity evidence.

    Patches ``_resolve_with_llm`` in ``graphiti_core.utils.maintenance.node_operations``
    to intercept the ``NodeResolutions`` returned by the LLM.  For each merge decision
    (``duplicate_candidate_id >= 0``), the gate applies two checks:

    1. **Name-level identity evidence** — exact match, substring, acronym, or ≥50%
       token Jaccard between extracted and candidate entity names.  If none exists,
       the merge is vetoed.

    2. **Edge-consistency invariant** — if the cached edges' fact text mentions the
       extracted entity name but *not* the candidate entity name, the fact contradicts
       the merge (e.g. fact "Rachel moved to the suburbs" mentions "suburbs" but not
       "Chicago").  This veto fires even when name-level evidence exists, as it
       signals the LLM is merging entities that the extraction itself distinguished.

    Every override is logged for analysis.

    This is defense-in-depth behind temperature=0 and the prompt patch.  It catches
    the residual failure mode where the LLM decides two names are the same entity
    despite zero lexical relationship (e.g. "the suburbs" → "Chicago").
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from graphiti_core.prompts.dedupe_nodes import NodeResolutions

        if getattr(_no_module, "_menhir_identity_gate_patched", False):
            return

        _original_resolve_with_llm = _no_module._resolve_with_llm

        async def _gated_resolve_with_llm(
            llm_client,
            extracted_nodes,
            indexes,
            state,
            episode=None,
            previous_episodes=None,
            entity_types=None,
        ):
            # Capture the original generate_response to intercept the LLM output
            _orig_gen = llm_client.generate_response

            # Read the combined-extraction edge cache (populated by the Menhir
            # combined-extraction patch before node resolution runs).  This is
            # read-only — the cache is consumed later by _extract_edges_from_combined_cache.
            cached_edges: list[Any] | None = None
            cached = _combined_extraction_cache.get()
            if cached is not None:
                cached_edges = cached[1]  # tuple is (episode_key, edges)

            async def _intercepted_gen(messages, response_model=None, **gen_kwargs):
                resp = await _orig_gen(messages, response_model=response_model, **gen_kwargs)
                prompt_name = gen_kwargs.get("prompt_name", "")

                if "dedupe_nodes" not in prompt_name:
                    return resp

                # resp is the raw dict the LLM returned -- it has NOT been through
                # PatchedNodeResolutions yet, so nothing has validated its shape. A model
                # returning `entity_resolutions: ["Alice"]` (bare strings, not objects)
                # reaches this gate intact and used to raise AttributeError straight up
                # through add_episode, failing the whole episode: its content lands in the
                # graph with no entities, add_memory still reports success, and recall can
                # never see it again. The type guards below mirror the fail-safe already in
                # PatchedNodeResolutions._drop_degenerate: skip what cannot be read, treat
                # an uncoercible duplicate_candidate_id as -1 ("no duplicate").
                if not isinstance(resp, dict):
                    return resp
                resolutions = resp.get("entity_resolutions") or []
                if not isinstance(resolutions, list):
                    return resp

                # Build lookup: extracted node id → name
                llm_extracted_nodes = [
                    extracted_nodes[i] for i in state.unresolved_indices
                ]
                extracted_by_id = {
                    i: node.name for i, node in enumerate(llm_extracted_nodes)
                }

                # Build lookup: candidate_id → name
                candidate_by_id = {
                    i: node.name for i, node in enumerate(indexes.existing_nodes)
                }

                overrides = []
                for resolution in resolutions:
                    if not isinstance(resolution, dict):
                        continue  # unusable entry; leave the LLM's output untouched
                    try:
                        dup_id = int(resolution.get("duplicate_candidate_id", -1))
                    except (TypeError, ValueError):
                        continue  # fail-safe: "no duplicate", nothing for the gate to veto
                    if dup_id < 0:
                        continue  # already "new entity"

                    try:
                        ext_id = int(resolution.get("id"))
                    except (TypeError, ValueError):
                        ext_id = None  # unusable index; fall back to the reported name
                    ext_name = extracted_by_id.get(ext_id, str(resolution.get("name") or ""))
                    cand_name = candidate_by_id.get(dup_id, "")

                    veto_reason = ""

                    # Check 1: name-level identity evidence
                    if not _has_positive_identity_evidence(ext_name, cand_name):
                        veto_reason = "no positive identity evidence"

                    # Check 2: edge-consistency invariant
                    # If cached edges mention the extracted name but NOT the
                    # candidate name, the fact text contradicts the merge.
                    if not veto_reason and cached_edges:
                        ext_facts = _edge_facts_mention(ext_name, cached_edges)
                        if ext_facts:
                            cand_facts = _edge_facts_mention(cand_name, cached_edges)
                            if not cand_facts:
                                veto_reason = (
                                    f"edge-consistency: facts mention {ext_name!r} "
                                    f"but not {cand_name!r}"
                                )

                    if veto_reason:
                        overrides.append({
                            "extracted_name": ext_name,
                            "candidate_name": cand_name,
                            "original_dup_id": dup_id,
                            "reason": veto_reason,
                        })
                        resolution["duplicate_candidate_id"] = -1

                if overrides:
                    ep_content = episode.content[:120] if episode else ""
                    for ov in overrides:
                        _identity_gate_logger.warning(
                            "Identity gate VETO: %r merged into %r by LLM — %s. "
                            "Overriding to new entity. Episode: %s",
                            ov["extracted_name"],
                            ov["candidate_name"],
                            ov["reason"],
                            ep_content,
                        )

                return resp

            llm_client.generate_response = _intercepted_gen
            try:
                await _original_resolve_with_llm(
                    llm_client, extracted_nodes, indexes, state,
                    episode, previous_episodes, entity_types,
                )
            finally:
                llm_client.generate_response = _orig_gen

        _no_module._resolve_with_llm = _gated_resolve_with_llm  # type: ignore[assignment]
        _no_module._menhir_identity_gate_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti dedup identity gate patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti dedup identity gate: %s", exc)


# ---------------------------------------------------------------------------
# Structural-node isolation and attribute preservation
# ---------------------------------------------------------------------------

#: Branch labels for one extracted node's deterministic-resolution attempt. These name the
#: mechanism the RCA identified: with 66 exact-name `user` nodes against a 15-candidate window,
#: `unique_exact_bind` became arithmetically unreachable and every extraction took
#: `multiple_exact_llm`, where a `duplicate_candidate_id = -1` mints another fork. Counting the
#: branches is what makes a recurrence attributable instead of inferred.
_DEDUP_BRANCHES = (
    "unique_exact_bind",
    "multiple_exact_llm",
    "entropy_guard_skip",
    "fuzzy_bind",
    "no_exact_llm",
    "no_candidates_new",
)


def _classify_dedup_branches(extracted_nodes: Any, indexes: Any, before: set[int], state: Any) -> dict[str, int]:
    """Classify each node's resolution branch from the resolver's own inputs and outputs.

    Derived by observation rather than by editing graphiti's function: the exact-match count comes
    from the same index the resolver consults, and resolution is read from the state it wrote.
    """
    # The exact-name normalizer lives in node_operations; the entropy/fuzzy helpers live in
    # dedup_helpers. Importing either from the wrong module silently disables a branch, so both
    # are taken from where 0.29.3 actually defines them.
    from graphiti_core.utils.maintenance import dedup_helpers as _dh
    from graphiti_core.utils.maintenance import node_operations as _no

    counts = {name: 0 for name in _DEDUP_BRANCHES}
    for idx, node in enumerate(extracted_nodes):
        try:
            exact = len(indexes.normalized_existing.get(_no._normalize_string_exact(node.name), []))
            resolved = state.resolved_nodes[idx] is not None
            if exact == 1 and resolved:
                counts["unique_exact_bind"] += 1
            elif exact > 1:
                counts["multiple_exact_llm"] += 1
            elif resolved:
                counts["fuzzy_bind"] += 1
            elif not _dh._has_high_entropy(_dh._normalize_name_for_fuzzy(node.name)):
                counts["entropy_guard_skip"] += 1
            elif idx in set(state.unresolved_indices) - before:
                counts["no_exact_llm"] += 1
            else:
                counts["no_candidates_new"] += 1
        except Exception:  # noqa: BLE001 - never let instrumentation break resolution
            continue
    return counts


def _patch_graphiti_dedup_branch_telemetry() -> None:
    """Record which deterministic-resolution branch each ordinary node took.

    Wraps `_resolve_with_similarity` without altering its logic: it runs untouched, and the
    classification is computed from its inputs and the state it produced. Any failure inside the
    instrumentation is swallowed, because a telemetry defect must never fail an ingest.
    """
    try:
        from graphiti_core.utils.maintenance import node_operations as _no_module
    except ImportError:
        logger.warning("Graphiti node_operations unavailable; dedup branch telemetry not applied")
        return

    if getattr(_no_module._resolve_with_similarity, "_menhir_branch_telemetry", False):
        return

    _original = _no_module._resolve_with_similarity

    def _instrumented(extracted_nodes, indexes, state):
        before = set(getattr(state, "unresolved_indices", []) or [])
        _original(extracted_nodes, indexes, state)
        try:
            counts = _classify_dedup_branches(extracted_nodes, indexes, before, state)
            if not any(counts.values()):
                return
            scores = [
                len(indexes.normalized_existing.get(k, []))
                for k in getattr(indexes, "normalized_existing", {})
            ]
            from menhir.infrastructure.telemetry.recorders import record_lifecycle_event

            record_lifecycle_event(
                component="graphiti_dedup",
                event="deterministic_resolution_branches",
                state="observed",
                episode_uuid=_current_episode_key(),
                details={
                    **counts,
                    "extracted_node_count": len(extracted_nodes),
                    "candidate_name_buckets": len(scores),
                    "max_exact_matches_for_one_name": max(scores) if scores else 0,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("Dedup branch telemetry failed", exc_info=True)

    _instrumented._menhir_branch_telemetry = True  # type: ignore[attr-defined]
    _no_module._resolve_with_similarity = _instrumented  # type: ignore[assignment]


def _current_episode_key() -> str | None:
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return None
    return getattr(receipt, "episode_key", None) or None if receipt is not None else None


def _stamp_canonical_self(node: Any, identity: Any) -> Any:
    """Put the canonical markers on the node that will be persisted.

    Graphiti writes `attributes` into the node's property map, and the generic ingest metadata
    stamp supplies neither marker. Without this the FIRST canonical node in a namespace is created
    without `is_self`/`entity_role`, so every reader that identifies the human structurally --
    fork detection, census, migration disposition -- would not recognize the node this change
    just created.
    """
    try:
        attributes = getattr(node, "attributes", None)
        if attributes is None:
            return node
        attributes["is_self"] = True
        attributes["entity_role"] = "self"
        if identity is not None and getattr(identity, "namespace", ""):
            attributes["namespace"] = identity.namespace
    except Exception:  # noqa: BLE001 - never fail resolution on a metadata stamp
        logger.exception("Could not stamp canonical-self markers")
    return node


async def _existing_canonical_node(clients: Any, extracted: Any, identity: Any) -> Any:
    """Return the persisted canonical self node, or a stamped *extracted* when none exists yet.

    First trusted-self episode in a namespace: nothing is stored, so the extracted node IS the
    canonical node and creating it is correct -- but it must carry the canonical markers, which
    is why this is the authoritative persistence boundary for them.

    Every episode after that: the stored node carries state the extraction does not have, and must
    be the object graphiti writes back.

    **Only a genuinely absent node falls back to the extracted object.** Graphiti persists a
    resolved node with `SET n = $entity_data`, which REPLACES the property map, so treating a
    transient driver or database failure as "absent" would let a later successful write erase the
    canonical node's markers, provenance, flags and accumulated summary. An operational failure
    must fail the episode, which is retryable, rather than silently degrade to a sparse overwrite.
    """
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    driver = getattr(clients, "driver", None)
    if driver is None:
        # An absent driver is an operational invariant failure, not evidence that the canonical
        # node does not exist. Falling back here would commit the sparse extracted node and let
        # graphiti's replacing save erase the stored one -- the same defect as swallowing a
        # transient read error, reached by a different door.
        raise RuntimeError(
            "canonical-self resolution requires a graph driver; refusing to substitute the "
            "extracted node for an unread canonical node"
        )
    try:
        stored = await EntityNode.get_by_uuid(driver, extracted.uuid)
    except NodeNotFoundError:
        return _stamp_canonical_self(extracted, identity)
    if identity is not None:
        from menhir.domain.namespace import namespace_to_group_id

        expected_group = namespace_to_group_id(identity.namespace)
        actual_group = getattr(stored, "group_id", None)
        if actual_group is None or str(actual_group) != expected_group:
            raise RuntimeError(
                f"stored canonical-self node {extracted.uuid!r} belongs to physical group "
                f"{actual_group!r}, expected {expected_group!r} for logical namespace "
                f"{identity.namespace!r}; refusing cross-namespace resolution"
            )
    return stored


def _measure_prompt_sections(
    batch_nodes: Any,
    candidate_nodes: Any,
    entity_types: Any = None,
    episode: Any = None,
    previous_episodes: Any = None,
) -> dict[str, int]:
    """Size every section of the dedupe prompt, by rebuilding the context graphiti serializes.

    An earlier version measured only names, labels and the sliced summary. That undercounts by
    whatever matters most: a candidate carrying a 1,000-character attribute was measured at nine
    characters, and the episode and previous-episode sections were not counted at all -- so the
    number could not answer the question it exists for, which is what is actually filling the
    dedupe prompt when a window saturates.

    This mirrors `_resolve_with_llm`'s four context keys, attributes included, and measures the
    JSON it would serialize. Mirroring drifts if graphiti changes that shape; a test pins the
    fields, and the counts are diagnostics, never control flow.
    """
    import json

    def _size(value: Any) -> int:
        try:
            return len(json.dumps(value, default=str))
        except Exception:  # noqa: BLE001 - measurement only
            return 0

    entity_types_dict = entity_types if isinstance(entity_types, dict) else {}
    try:
        from graphiti_core.utils.maintenance.node_operations import _get_entity_type_description
    except Exception:  # noqa: BLE001 - graphiti internal; absence must not break instrumentation
        _get_entity_type_description = None  # type: ignore[assignment]

    def _description(labels: Any) -> str:
        if _get_entity_type_description is None:
            return ""
        try:
            return str(_get_entity_type_description(labels, entity_types_dict) or "")
        except Exception:  # noqa: BLE001
            return ""

    batch_nodes = batch_nodes if isinstance(batch_nodes, (list, tuple)) else []
    candidate_nodes = candidate_nodes if isinstance(candidate_nodes, (list, tuple)) else []
    extracted_context = [
        {
            "id": i,
            "name": getattr(n, "name", ""),
            "entity_type": getattr(n, "labels", []),
            "entity_type_description": _description(getattr(n, "labels", [])),
        }
        for i, n in enumerate(batch_nodes)
    ]
    existing_context = [
        {
            **(getattr(c, "attributes", None) or {}),
            "candidate_id": i,
            "name": getattr(c, "name", ""),
            "entity_types": getattr(c, "labels", []),
            "summary": (getattr(c, "summary", "") or "")[:120],
        }
        for i, c in enumerate(candidate_nodes)
    ]
    episode_content = getattr(episode, "content", "") if episode is not None else ""
    previous_context = []
    for ep in (previous_episodes if isinstance(previous_episodes, (list, tuple)) else []):
        valid_at = getattr(ep, "valid_at", None)
        try:
            timestamp = valid_at.isoformat() if valid_at else None
        except Exception:  # noqa: BLE001 - measurement must not break ingest
            timestamp = None
        previous_context.append(
            {"content": getattr(ep, "content", ""), "timestamp": timestamp}
        )

    entity_chars = _size(extracted_context)
    candidate_chars = _size(existing_context)
    episode_chars = _size(episode_content)
    previous_chars = _size(previous_context)

    return {
        "entity_count": len(batch_nodes),
        "entity_chars": entity_chars,
        "candidate_count": len(candidate_nodes),
        "candidate_chars": candidate_chars,
        "episode_chars": episode_chars,
        "previous_episode_count": len(previous_context),
        "previous_episode_chars": previous_chars,
        "total_chars": entity_chars + candidate_chars + episode_chars + previous_chars,
    }


def _record_resolution_outcomes(
    clients: Any,
    extracted_nodes: Any,
    candidates_by_extracted: Any,
    escalated: list[int],
    state: Any,
    pre_resolved_indices: set[int],
    prompt_sections: list[dict[str, int]] | None = None,
) -> None:
    """Record the OUTCOME of the full resolution lifecycle, including the LLM paths.

    The deterministic-branch wrapper around `_resolve_with_similarity` cannot see what the LLM
    decided, so on its own it leaves the exact branch the RCA implicated -- an escalation that
    returns `duplicate_candidate_id = -1` and mints another node -- unrecorded. This closes that:
    an escalated node resolved onto a DIFFERENT uuid is `llm_selected_candidate`; one resolved
    onto itself is `llm_selected_new`, which is the fork-creating outcome.

    **Per-candidate cosine scores are deliberately absent.** Graphiti's search ranks by score and
    then discards it: `get_entity_node_return_query` omits `name_embedding` from the projection and
    `get_entity_node_from_record` pops it from `attributes`, so every candidate arrives with
    `name_embedding=None` on the production Neo4j path. A previous revision measured the cosine
    from the two embeddings, which meant it silently measured nothing in production while looking
    like a metric. Recovering real bounds requires either `load_name_embedding()` per candidate --
    a per-node round trip in the ingest hot path -- or patching `node_similarity_search` to return
    its score. The window-saturation signature the RCA depends on remains visible without them, in
    `candidate_count_max` and the `multiple_exact_llm` branch counter.
    """
    try:
        llm_selected_candidate = 0
        llm_selected_new = 0
        for idx in escalated:
            resolved = state.resolved_nodes[idx]
            if resolved is None:
                llm_selected_new += 1
                continue
            extracted_uuid = str(getattr(extracted_nodes[idx], "uuid", "") or "")
            resolved_uuid = str(getattr(resolved, "uuid", "") or "")
            if resolved_uuid and resolved_uuid != extracted_uuid:
                llm_selected_candidate += 1
            else:
                llm_selected_new += 1

        candidate_counts = [len(c or []) for c in candidates_by_extracted]
        no_candidates_new = sum(
            1
            for idx, count in enumerate(candidate_counts)
            if count == 0 and idx not in pre_resolved_indices
        )

        # Embedding identity/dimension: what the candidate window was actually built from. A
        # dimension or model change silently alters which candidates are reachable at all.
        sections = list(prompt_sections or [])

        embedder = getattr(clients, "embedder", None)
        embedder_config = getattr(embedder, "config", None)
        embedding_model = str(getattr(embedder_config, "embedding_model", "") or "") or None
        dimensions = [
            len(v)
            for v in (getattr(n, "name_embedding", None) for n in extracted_nodes)
            if isinstance(v, (list, tuple))
        ]

        from menhir.infrastructure.telemetry.recorders import record_lifecycle_event

        record_lifecycle_event(
            component="graphiti_dedup",
            event="resolution_outcomes",
            state="observed",
            episode_uuid=_current_episode_key(),
            details={
                "extracted_node_count": len(extracted_nodes),
                "pre_resolved_self": len(pre_resolved_indices),
                "escalated_to_llm": len(escalated),
                "llm_selected_candidate": llm_selected_candidate,
                "llm_selected_new": llm_selected_new,
                "no_candidates_new": no_candidates_new,
                "unresolved_after_llm": sum(
                    1 for idx in escalated if state.resolved_nodes[idx] is None
                ),
                "candidate_count_min": min(candidate_counts) if candidate_counts else 0,
                "candidate_count_max": max(candidate_counts) if candidate_counts else 0,
                "llm_prompt_batches": len(sections),
                "llm_prompt_entity_chars_max": (
                    max((s["entity_chars"] for s in sections), default=0)
                ),
                "llm_prompt_candidate_chars_max": (
                    max((s["candidate_chars"] for s in sections), default=0)
                ),
                "llm_prompt_candidate_count_max": (
                    max((s["candidate_count"] for s in sections), default=0)
                ),
                "llm_prompt_total_chars_max": (
                    max((s["total_chars"] for s in sections), default=0)
                ),
                "llm_prompt_episode_chars_max": (
                    max((s["episode_chars"] for s in sections), default=0)
                ),
                "embedding_model": embedding_model,
                "embedding_dimension": max(dimensions) if dimensions else None,
            },
        )
    except Exception:  # noqa: BLE001 - instrumentation must never fail resolution
        logger.debug("Resolution outcome telemetry failed", exc_info=True)


def _active_self_identity() -> Any:
    """The identity context for the current episode, if binding ran."""
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return None
    return getattr(receipt, "self_identity", None) if receipt is not None else None


def _pre_resolved_self_uuid() -> str | None:
    """The canonical self uuid bound for the current episode, if binding ran and succeeded.

    Read from the task-local extraction receipt rather than passed down, because Graphiti owns
    the call signature between extraction and resolution.
    """
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001 - resolution must never fail on instrumentation
        return None
    result = getattr(receipt, "self_bind_result", None) if receipt is not None else None
    if result is None or not getattr(result, "bound", False):
        return None
    return getattr(result, "self_uuid", None)


def _canonical_self_candidate_filter_enabled() -> bool:
    """Candidate isolation mutates resolution, so it belongs to ENFORCE only.

    OFF must reproduce the old resolver and OBSERVE must measure without changing ingest. The
    receipt stores a StrEnum, but compare its string form so this helper stays decoupled from the
    binding module and fails closed when no receipt exists.
    """
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return False
    return bool(
        receipt is not None
        and str(getattr(receipt, "self_bind_mode", "") or "") == "enforce"
    )


def _is_canonical_self_candidate(node: Any, identity: Any) -> bool:
    """Protect canonical self from every ordinary Graphiti resolution path.

    A declaration-bound node is removed from candidate search entirely. Every node that remains
    searchable is therefore unproven and must not reach canonical self through exact-name,
    similarity, an LLM choice, or ``existing_nodes_override``. Markers cover canonical nodes from
    any namespace; the deterministic UUID covers an incompletely stamped node in this namespace.
    """
    attributes = getattr(node, "attributes", None)
    if isinstance(attributes, dict) and (
        attributes.get("is_self") is True
        or str(attributes.get("entity_role") or "").strip().casefold() == "self"
    ):
        return True
    expected_uuid = str(getattr(identity, "self_uuid", "") or "")
    return bool(expected_uuid) and str(getattr(node, "uuid", "") or "") == expected_uuid


def _is_structural_graphiti_candidate(node: Any) -> bool:
    """Return whether a Graphiti candidate belongs to Menhir's structure graph.

    Graphiti materializes every non-core Neo4j property under ``EntityNode.attributes``.
    Structural nodes therefore remain distinguishable during candidate resolution even though
    they share the generic ``:Entity`` label with semantic memory nodes.
    """
    attributes = getattr(node, "attributes", None)
    return isinstance(attributes, dict) and attributes.get("structure_role") is not None


def _patch_graphiti_structural_candidate_isolation() -> bool:
    """Prevent semantic enrichment from resolving onto structural ``:Entity`` nodes.

    Graphiti's semantic candidate search has no knowledge of Menhir's structural/semantic
    boundary.  An extracted project or file name can therefore resolve to an existing structure
    node and send that node through Graphiti's hydration + replacement-save path.  Filter after
    candidate collection so both search results and ``existing_nodes_override`` inputs obey the
    boundary, while semantic candidates retain their original order.
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module

        if getattr(_no_module, "_menhir_structural_candidate_isolation_patched", False):
            return True

        _original_collect_candidate_nodes = _no_module._collect_candidate_nodes

        async def _collect_non_structural_candidates(
            clients,
            extracted_nodes,
            existing_nodes_override,
        ):
            candidates_by_extracted = await _original_collect_candidate_nodes(
                clients, extracted_nodes, existing_nodes_override
            )
            dropped = 0
            filtered: list[list[Any]] = []
            for candidates in candidates_by_extracted:
                eligible = [
                    candidate
                    for candidate in candidates
                    if not _is_structural_graphiti_candidate(candidate)
                ]
                dropped += len(candidates) - len(eligible)
                filtered.append(eligible)
            if dropped:
                logger.info(
                    "Excluded %d structural node candidate(s) from Graphiti semantic dedup",
                    dropped,
                )
            return filtered

        _no_module._collect_candidate_nodes = (  # type: ignore[assignment]
            _collect_non_structural_candidates
        )
        _no_module._menhir_structural_candidate_isolation_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti structural candidate isolation patch applied")
        return True
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti structural candidate isolation: %s", exc)
        return False


def _patch_graphiti_untyped_attribute_preservation() -> None:
    """Preserve existing properties when Graphiti has no typed attribute schema.

    Graphiti 0.29.2 returns ``{}`` for an untyped node, assigns that result to
    ``EntityNode.attributes``, then persists the node with ``SET n = node``.  Any properties owned
    outside Graphiti are erased.  Keeping a copy of the existing attributes closes that generic
    replacement-save hazard even for non-structural nodes and provides defense in depth behind the
    structural candidate filter.
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module

        if getattr(_no_module, "_menhir_untyped_attribute_preservation_patched", False):
            return

        _original_extract_entity_attributes = _no_module._extract_entity_attributes

        async def _extract_entity_attributes_preserving_existing(
            llm_client,
            node,
            episode,
            previous_episodes,
            entity_type,
        ):
            model_fields = getattr(entity_type, "model_fields", None)
            if entity_type is None or not model_fields:
                return dict(getattr(node, "attributes", None) or {})
            return await _original_extract_entity_attributes(
                llm_client,
                node,
                episode,
                previous_episodes,
                entity_type,
            )

        _no_module._extract_entity_attributes = (  # type: ignore[assignment]
            _extract_entity_attributes_preserving_existing
        )
        _no_module._menhir_untyped_attribute_preservation_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti untyped attribute preservation patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti untyped attribute preservation: %s", exc)


def _patch_graphiti_adaptive_dedupe() -> bool:
    """Split oversized node-deduplication requests without reducing candidate quality.

    Graphiti 0.29 retrieves up to 15 candidates for every extracted entity, merges all
    candidates for all unresolved entities, and sends one LLM request.  Large but valid
    episodes can therefore fan out into a request hundreds of times larger than the
    episode itself.  Keep Graphiti's normal one-request path, but when the assembled
    request exceeds the local/provider context limit, bisect the unresolved entities
    and retry each half with only the candidates retrieved for that half.

    Candidate search and deterministic similarity resolution still run exactly once.
    The fallback changes neither the per-entity candidate limit nor candidate order.
    """

    try:
        import graphiti_core.graphiti as _graphiti_module
        import graphiti_core.utils.bulk_utils as _bulk_module
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from graphiti_core.utils.maintenance.dedup_helpers import DedupResolutionState

        if getattr(_no_module, "_menhir_adaptive_dedupe_patched", False):
            return True

        async def _adaptive_resolve_extracted_nodes(
            clients,
            extracted_nodes,
            episode=None,
            previous_episodes=None,
            entity_types=None,
            existing_nodes_override=None,
        ):
            llm_client = clients.llm_client

            # A node already bound to the deterministic canonical-self uuid is authoritative by
            # construction: trusted episode metadata proved the author, so there is nothing for
            # similarity or an LLM to decide. It is withheld from _collect_candidate_nodes
            # entirely -- not merely skipped afterwards -- because the cosine search IS the
            # mechanism that fragmented this identity: the `user` candidate window saturates
            # with exact-name matches, making the deterministic single-match branch unreachable
            # and routing every extraction to the LLM.
            prompt_sections: list[dict[str, int]] = []
            pre_resolved_indices: set[int] = set()
            _bound_uuid = _pre_resolved_self_uuid()
            if _bound_uuid:
                pre_resolved_indices = {
                    idx
                    for idx, node in enumerate(extracted_nodes)
                    if str(getattr(node, "uuid", "") or "") == _bound_uuid
                }
                logger.info(
                    "Canonical-self resolver pre-resolved uuid=%s matches=%d",
                    _bound_uuid,
                    len(pre_resolved_indices),
                )

            searchable = [n for i, n in enumerate(extracted_nodes) if i not in pre_resolved_indices]
            candidate_filter_enabled = _canonical_self_candidate_filter_enabled()
            identity = _active_self_identity() if candidate_filter_enabled else None
            if candidate_filter_enabled:
                undeclared_canonical_nodes = [
                    node
                    for node in searchable
                    if _is_canonical_self_candidate(node, identity)
                ]
                if undeclared_canonical_nodes:
                    raise RuntimeError(
                        "undeclared extracted node carries canonical-self identity in enforce "
                        "mode; refusing ordinary Graphiti resolution"
                    )
            searched = await _no_module._collect_candidate_nodes(
                clients,
                searchable,
                existing_nodes_override,
            )
            # The declaration-bound node never enters search. Conversely, an ordinary searchable
            # node must never acquire the canonical UUID through Graphiti's name/similarity/LLM
            # path. Endpoint closure can retain an ordinary node named `user`; without this filter
            # a unique exact match would silently turn that name back into identity authority.
            if candidate_filter_enabled:
                canonical_candidates_excluded = 0
                protected_search_results: list[list[Any]] = []
                for candidates in searched:
                    eligible = [
                        candidate
                        for candidate in candidates
                        if not _is_canonical_self_candidate(candidate, identity)
                    ]
                    canonical_candidates_excluded += len(candidates) - len(eligible)
                    protected_search_results.append(eligible)
                searched = protected_search_results
                if canonical_candidates_excluded:
                    logger.info(
                        "Excluded %d canonical-self candidate(s) from undeclared Graphiti dedup",
                        canonical_candidates_excluded,
                    )
            # Realign to the full extracted list; pre-resolved nodes get no candidates.
            _searched_iter = iter(searched)
            candidate_nodes_by_extracted = [
                [] if i in pre_resolved_indices else next(_searched_iter)
                for i in range(len(extracted_nodes))
            ]

            state = DedupResolutionState(
                resolved_nodes=[None] * len(extracted_nodes),
                uuid_map={},
                unresolved_indices=[],
            )

            for idx in pre_resolved_indices:
                node = extracted_nodes[idx]
                # Commit the EXISTING canonical node when there is one, not the freshly extracted
                # object. Graphiti persists a resolved node with `SET n = $entity_data`, which
                # REPLACES the property map rather than merging it, so committing the extraction
                # would wipe the canonical node's `is_self`, `entity_role`, `namespace`,
                # `user_flagged`, provenance and accumulated summary on every subsequent self
                # episode. The ordinary path avoids this precisely because `_promote_resolved_node`
                # returns the hydrated database node; the bypass has to do the same.
                #
                # This is a direct uuid fetch, not candidate acquisition: no cosine search, no
                # exact/fuzzy resolution, no dedup LLM, no identity gate. D4 is preserved.
                state.resolved_nodes[idx] = await _existing_canonical_node(
                    clients, node, _active_self_identity()
                )
                state.uuid_map[node.uuid] = node.uuid

            for idx, (node, candidates) in enumerate(
                zip(extracted_nodes, candidate_nodes_by_extracted, strict=True)
            ):
                if idx in pre_resolved_indices or not candidates:
                    continue

                indexes = _no_module._build_candidate_indexes(candidates)
                local_state = DedupResolutionState(
                    resolved_nodes=[None], uuid_map={}, unresolved_indices=[]
                )
                _no_module._resolve_with_similarity([node], indexes, local_state)
                if local_state.resolved_nodes[0] is not None:
                    _no_module._commit_resolution(
                        state,
                        local_state.resolved_nodes[0],
                        local_state.uuid_map,
                        local_state.duplicate_pairs,
                        idx,
                    )
                    continue

                state.unresolved_indices.append(idx)

            async def _resolve_batch(indices: list[int], depth: int = 0) -> None:
                candidate_nodes = _no_module._merge_candidate_nodes(
                    [
                        candidate
                        for idx in indices
                        for candidate in candidate_nodes_by_extracted[idx]
                    ],
                    None,
                )
                batch_state = DedupResolutionState(
                    resolved_nodes=[None] * len(extracted_nodes),
                    uuid_map={},
                    unresolved_indices=list(indices),
                )
                try:
                    prompt_sections.append(
                        _measure_prompt_sections(
                            [extracted_nodes[i] for i in indices],
                            candidate_nodes,
                            entity_types,
                            episode,
                            previous_episodes,
                        )
                    )
                except Exception:  # noqa: BLE001 - instrumentation only
                    logger.debug("Prompt-section measurement failed", exc_info=True)

                try:
                    await _no_module._resolve_with_llm(
                        llm_client,
                        extracted_nodes,
                        _no_module._build_candidate_indexes(candidate_nodes),
                        batch_state,
                        episode,
                        previous_episodes,
                        entity_types,
                    )
                except GraphitiRequestTooLargeError:
                    if len(indices) <= 1:
                        logger.error(
                            "Graphiti node-dedupe request remains oversized for one entity "
                            "candidate_count=%d split_depth=%d",
                            len(candidate_nodes),
                            depth,
                        )
                        raise

                    midpoint = len(indices) // 2
                    left = indices[:midpoint]
                    right = indices[midpoint:]
                    logger.warning(
                        "Graphiti node-dedupe request oversized; splitting entity batch "
                        "entities=%d candidates=%d split_depth=%d left=%d right=%d",
                        len(indices),
                        len(candidate_nodes),
                        depth,
                        len(left),
                        len(right),
                    )
                    await _resolve_batch(left, depth + 1)
                    await _resolve_batch(right, depth + 1)
                    return

                for idx in indices:
                    resolved_node = batch_state.resolved_nodes[idx]
                    if resolved_node is not None:
                        state.resolved_nodes[idx] = resolved_node
                state.uuid_map.update(batch_state.uuid_map)
                state.duplicate_pairs.extend(batch_state.duplicate_pairs)

            escalated = list(state.unresolved_indices)
            if state.unresolved_indices:
                await _resolve_batch(list(state.unresolved_indices))
            _record_resolution_outcomes(
                clients, extracted_nodes, candidate_nodes_by_extracted, escalated,
                state, pre_resolved_indices, prompt_sections,
            )

            if not state.unresolved_indices and not any(candidate_nodes_by_extracted):
                logger.debug("No semantic dedup candidates found; keeping all extracted nodes as new")

            for idx, node in enumerate(extracted_nodes):
                if state.resolved_nodes[idx] is None:
                    state.resolved_nodes[idx] = node
                    state.uuid_map[node.uuid] = node.uuid

            receipt = get_extraction_receipt()
            if receipt is not None:
                for extracted_node, resolved_node in zip(
                    extracted_nodes, state.resolved_nodes, strict=True
                ):
                    if resolved_node is None:
                        continue
                    receipt.resolved_node_identity_by_extracted_uuid[
                        str(extracted_node.uuid)
                    ] = (
                        str(resolved_node.uuid),
                        str(resolved_node.name or ""),
                        tuple(sorted(str(label) for label in (resolved_node.labels or []))),
                    )

            logger.debug(
                "Resolved nodes with adaptive dedupe: %s",
                [node.uuid for node in state.resolved_nodes if node is not None],
            )
            return (
                [node for node in state.resolved_nodes if node is not None],
                state.uuid_map,
                state.duplicate_pairs,
            )

        _no_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _graphiti_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _bulk_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _no_module._menhir_adaptive_dedupe_patched = True
        logger.debug("Graphiti adaptive node-dedupe batching patch applied")
        return True
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti adaptive node dedupe: %s", exc)
        return False
