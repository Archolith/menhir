"""Dedup branch/outcome telemetry and dedupe prompt-section measurement."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
