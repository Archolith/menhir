"""Graphiti record and constructor None-coercion patches with bounded malformed-entity logging."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
