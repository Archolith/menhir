"""Deterministic, inspection-only typed recall packet for Recall Lab.

This module intentionally lives in ``menhir.explorer``.  It formats the existing
live-graph projection for operator inspection and does not participate in
production recall, ranking, ingestion, or persistence.
"""

from __future__ import annotations

from menhir.explorer.recall_packet_prototype_build import (
    PACKET_VERSION as PACKET_VERSION,
    _SECTION_DEFINITIONS as _SECTION_DEFINITIONS,
    _USAGE_POLICY as _USAGE_POLICY,
    _event_assertion_for_entry as _event_assertion_for_entry,
    _grounding_quote as _grounding_quote,
    _latest as _latest,
    _render_entry as _render_entry,
    _sources_for_assertion_ids as _sources_for_assertion_ids,
    _text as _text,
    build_typed_recall_packet as build_typed_recall_packet,
    render_typed_recall_packet as render_typed_recall_packet,
)
from menhir.explorer.recall_packet_prototype_query import (
    DEFAULT_QUERY_PACKET_MAX_CHARS as DEFAULT_QUERY_PACKET_MAX_CHARS,
    QUERY_FILTERED_PACKET_VERSION as QUERY_FILTERED_PACKET_VERSION,
    _EVENT_INTENT as _EVENT_INTENT,
    _HISTORY_INTENT as _HISTORY_INTENT,
    _STOP_WORDS as _STOP_WORDS,
    _TOKEN_RE as _TOKEN_RE,
    _authority_ids as _authority_ids,
    _compact as _compact,
    _entry_tokens as _entry_tokens,
    _identity_relevant as _identity_relevant,
    _query_compact as _query_compact,
    _relevant as _relevant,
    _render_query_filtered_packet as _render_query_filtered_packet,
    _retrieval_entry as _retrieval_entry,
    _scalar_key as _scalar_key,
    _section_map as _section_map,
    _selected_typed_entry as _selected_typed_entry,
    _temporal_fields as _temporal_fields,
    _tokens as _tokens,
    build_query_filtered_recall_packet as build_query_filtered_recall_packet,
)
