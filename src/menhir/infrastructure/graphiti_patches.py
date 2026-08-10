"""Public facade for focused Graphiti compatibility patch families."""

from menhir.infrastructure.graphiti_extraction_patches import (
    CombinedExtractionReceipt,
    _combined_extraction_cache,
    _extract_edges_from_combined_cache,
    _extract_nodes_combined_for_add_episode,
    _patch_graphiti_combined_extraction,
    _patch_graphiti_combined_extraction_models,
    begin_extraction_receipt,
    clear_extraction_receipt,
    get_extraction_receipt,
    is_policy_empty_extraction,
)
from menhir.infrastructure.graphiti_llm_patches import (
    _LOCAL_MODEL_MAX_RETRIES,
    _TRUNCATION_CHAR_THRESHOLD,
    _looks_like_truncation,
    _patch_graphiti_openai_generic_client,
    _truncation_offset,
)
from menhir.infrastructure.graphiti_model_patches import (
    _MALFORMED_ENTITY_DATES_LOGGED,
    _MALFORMED_ENTITY_GROUP_IDS_LOGGED,
    _edge_facts_mention,
    _has_positive_identity_evidence,
    _is_structural_graphiti_candidate,
    _patch_graphiti_adaptive_dedupe,
    _patch_graphiti_dedup_identity_gate,
    _patch_graphiti_dedup_prompt,
    _patch_graphiti_dedupe_resolutions,
    _patch_graphiti_edge_none_fields,
    _patch_graphiti_entity_extraction,
    _patch_graphiti_entity_record_group_id,
    _patch_graphiti_node_summary_none,
    _patch_graphiti_none_replace,
    _patch_graphiti_prompt_json,
    _patch_graphiti_structural_candidate_isolation,
    _patch_graphiti_summarize,
    _patch_graphiti_untyped_attribute_preservation,
    _safe_to_prompt_json,
)

__all__ = [name for name in globals() if not name.startswith("__")]
