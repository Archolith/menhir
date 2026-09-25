"""Graphiti prompt, model-normalization, and deduplication compatibility patches."""

from __future__ import annotations

import importlib.metadata
import logging

from menhir.infrastructure.graphiti_extraction_patches import (
    _combined_extraction_cache,
    _extract_edges_from_combined_cache,
)
from menhir.infrastructure.graphiti_helpers import (
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
    check_graphiti_version,
)
from menhir.infrastructure.graphiti_llm_patches import GraphitiRequestTooLargeError
from menhir.infrastructure.graphiti_model_patches_canonical import (
    _active_self_identity,
    _canonical_self_candidate_filter_enabled,
    _existing_canonical_node,
    _is_canonical_self_candidate,
    _patch_graphiti_adaptive_dedupe,
    _pre_resolved_self_uuid,
    _stamp_canonical_self,
)
from menhir.infrastructure.graphiti_model_patches_candidates import (
    _is_structural_graphiti_candidate,
    _is_view_graphiti_candidate,
    _patch_graphiti_structural_candidate_isolation,
    _patch_graphiti_untyped_attribute_preservation,
)
from menhir.infrastructure.graphiti_model_patches_extraction import (
    _DEDUP_ANTI_CONFLATION_EXAMPLE,
    _patch_graphiti_dedup_prompt,
    _patch_graphiti_dedupe_resolutions,
    _patch_graphiti_entity_extraction,
)
from menhir.infrastructure.graphiti_model_patches_identity_gate import (
    _edge_facts_mention,
    _has_positive_identity_evidence,
    _identity_gate_logger,
    _patch_graphiti_dedup_identity_gate,
)
from menhir.infrastructure.graphiti_model_patches_prompt import (
    _GRAPHITI_PROMPT_MODULES,
    _MAX_PROMPT_NUMERIC_LIST_LEN,
    _is_embedding_value,
    _patch_graphiti_none_replace,
    _patch_graphiti_prompt_json,
    _patch_graphiti_summarize,
    _prompt_json_default,
    _safe_to_prompt_json,
    _strip_embeddings,
)
from menhir.infrastructure.graphiti_model_patches_records import (
    _EDGE_DROP_IF_NONE,
    _EDGE_REQUIRED_STR_FIELDS,
    _MALFORMED_ENTITY_DATES_LOGGED,
    _MALFORMED_ENTITY_GROUP_IDS_LOGGED,
    _MALFORMED_LOG_KEYS_MAX,
    _first_time_seen,
    _patch_graphiti_edge_none_fields,
    _patch_graphiti_entity_record_group_id,
    _patch_graphiti_node_summary_none,
)
from menhir.infrastructure.graphiti_model_patches_telemetry import (
    _DEDUP_BRANCHES,
    _classify_dedup_branches,
    _current_episode_key,
    _measure_prompt_sections,
    _patch_graphiti_dedup_branch_telemetry,
    _record_resolution_outcomes,
)

logger = logging.getLogger(__name__)

# Version guard - run once at import, matching the other patch modules (CF-87).
check_graphiti_version()
