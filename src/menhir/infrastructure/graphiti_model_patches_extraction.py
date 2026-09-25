"""Graphiti extraction and dedupe model patches tolerating degenerate LLM output."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
