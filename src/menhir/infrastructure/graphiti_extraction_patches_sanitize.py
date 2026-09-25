"""Sanitation of raw combined-extraction payloads before Graphiti validation and resolution.

Owns the per-row entity/edge normalizers and ``_sanitize_combined_payload`` (malformed-row
tolerance, marker quarantine, endpoint closure, echo suppression, titled-list membership).
Extracted verbatim from ``graphiti_extraction_patches``; that module re-exports everything here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from menhir.domain.self_identity import SUBJECT_ENDPOINT_MARKER_PREFIX
from menhir.infrastructure.graphiti_extraction_patches_endpoints import (
    _SELF_ENTITY_NAME,
    _SELF_FIRST_PERSON,
    _SELF_THIRD_PERSON,
    _ASSISTANT_POLICY_SELF_LABELS,
    _active_subject_marker,
    _edge_has_current_message_anchor,
    _episode_role,
    _is_reserved_subject_marker,
    _is_synthesizable_endpoint,
    _is_unresolved_self_like_endpoint,
    _normalize_endpoint_name,
    _subject_marker_guard_active,
)
from menhir.infrastructure.graphiti_extraction_patches_lists import _MEMBERSHIP_RELATION
from menhir.infrastructure.graphiti_extraction_patches_receipt import CombinedExtractionReceipt
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX

logger = logging.getLogger(__name__)


def _sanitize_combined_entity(item: Any) -> dict[str, Any] | None:
    """Normalize one raw extracted-entity row, or return None if unusable."""
    if not isinstance(item, dict):
        return None
    item = dict(item)
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        # Tolerate the Qwen/DeepSeek key variants the separate-path patch also handles.
        for alt in ("entity_name", "entity"):
            alt_val = item.get(alt)
            if isinstance(alt_val, str) and alt_val.strip():
                name = alt_val
                break
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        type_id = int(item.get("entity_type_id"))
    except (TypeError, ValueError):
        type_id = -1  # generic Entity (upstream maps out-of-range -> "Entity")
    return {"name": name.strip(), "entity_type_id": type_id}


def _sanitize_combined_edge(item: Any) -> dict[str, Any] | None:
    """Normalize one raw edge row, or return None when an indispensable field is missing."""
    if not isinstance(item, dict):
        return None
    item = dict(item)
    cleaned: dict[str, Any] = {}
    for key in ("source_entity_name", "target_entity_name", "relation_type", "fact"):
        val = item.get(key)
        if not isinstance(val, str) or not val.strip():
            return None  # missing/blank endpoint, relation, or fact -> drop this edge only
        cleaned[key] = val
    idx = item.get("episode_indices")
    if isinstance(idx, list):
        clean_idx = [i for i in idx if isinstance(i, int) and not isinstance(i, bool)]
        cleaned["episode_indices"] = clean_idx or [0]
    else:
        cleaned["episode_indices"] = [0]
    return cleaned


def _sanitize_combined_payload(
    data: Any,
    receipt: CombinedExtractionReceipt | None,
    episode_text: str,
) -> Any:
    """Sanitize a raw combined-extraction payload and close missing edge endpoints.

    Order (per remediation contract): record raw counts -> drop malformed edge rows
    -> normalize extracted entities -> add missing usable edge endpoints -> hand back
    to Graphiti for its normal resolution. Runs BEFORE ``CombinedExtraction`` is
    validated so a single malformed row cannot invalidate the whole batch, and BEFORE
    Graphiti's edge/orphan pruning so a legitimate edge is not dropped for lack of a
    listed endpoint.
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    raw_entities = data.get("extracted_entities")
    raw_edges = data.get("edges")
    raw_entities = raw_entities if isinstance(raw_entities, list) else []
    raw_edges = raw_edges if isinstance(raw_edges, list) else []

    if receipt is not None:
        receipt.raw_entity_count = len(raw_entities)
        receipt.raw_edge_count = len(raw_edges)

    entities: list[dict[str, Any]] = []
    entities_dropped = 0
    for item in raw_entities:
        norm = _sanitize_combined_entity(item)
        if norm is None:
            entities_dropped += 1
            continue
        marker = _active_subject_marker(receipt)
        if (
            _subject_marker_guard_active(receipt)
            and SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in norm["name"].casefold()
            and norm["name"] != marker
        ):
            # A stale, malformed, or model-invented reserved endpoint is never an ordinary entity.
            # Only the exact capability token on this task's receipt may survive sanitation.
            entities_dropped += 1
            continue
        entities.append(norm)

    edges: list[dict[str, Any]] = []
    edges_dropped = 0
    subject_marker_edges_suppressed = 0
    for item in raw_edges:
        norm = _sanitize_combined_edge(item)
        if norm is None:
            edges_dropped += 1
            continue
        marker = _active_subject_marker(receipt)
        if _subject_marker_guard_active(receipt) and any(
            _is_reserved_subject_marker(norm[key]) and norm[key] != marker
            for key in ("source_entity_name", "target_entity_name")
        ):
            edges_dropped += 1
            continue
        if _subject_marker_guard_active(receipt):
            endpoint_uses_marker = any(
                norm[key] == marker
                for key in ("source_entity_name", "target_entity_name")
            )
            marker_text = " ".join(
                norm[key] for key in ("relation_type", "fact")
            )
            marker_occurs_in_text = (
                SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in marker_text.casefold()
            )
            active_marker_occurs = bool(
                marker and marker.casefold() in marker_text.casefold()
            )
            foreign_marker_occurs = SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in (
                re.sub(re.escape(marker), "", marker_text, flags=re.IGNORECASE)
                if marker else marker_text
            ).casefold()
            if marker_occurs_in_text and (
                not endpoint_uses_marker or not active_marker_occurs or foreign_marker_occurs
            ):
                # A marker in prose without the exact marker endpoint has no authority path that
                # can scrub it before persistence. Drop the edge rather than leak a capability.
                edges_dropped += 1
                subject_marker_edges_suppressed += 1
                continue
        edges.append(norm)

    context_unsupported_edges = 0
    if (
        receipt is not None
        and receipt.relationless_repair_attempted
        and receipt.relationless_repair_context_texts
        and edges
    ):
        grounded_edges = [
            edge
            for edge in edges
            if _edge_has_current_message_anchor(edge, episode_text)
        ]
        context_unsupported_edges = len(edges) - len(grounded_edges)
        edges = grounded_edges

    known = {_normalize_endpoint_name(e["name"]) for e in entities}
    self_key = _normalize_endpoint_name(_SELF_ENTITY_NAME)
    is_assistant_turn = _episode_role(episode_text) == "assistant"
    _all_self_labels = bool(
        entities
        and not raw_edges
        and entities_dropped == 0
        and all(
            _normalize_endpoint_name(entity["name"])
            in _ASSISTANT_POLICY_SELF_LABELS
            for entity in entities
        )
    )
    assistant_self_only_relationless = bool(is_assistant_turn and _all_self_labels)
    synthesized = 0
    self_like_endpoints_retained = 0
    self_echo_edges = 0
    surviving_edges: list[dict[str, Any]] = []
    for edge in edges:
        edge_is_self_echo = False
        for endpoint_key in ("source_entity_name", "target_entity_name"):
            endpoint_name = edge[endpoint_key]
            norm_key = _normalize_endpoint_name(endpoint_name)
            if is_assistant_turn and (
                norm_key in _SELF_THIRD_PERSON or norm_key in _SELF_FIRST_PERSON
            ):
                # This is the assistant restating a fact the human already gave first-hand. The
                # decision is made on ROLE + LABEL alone and is tested BEFORE `known` membership,
                # because enforcement used to rely on leaving the endpoint unbound so graphiti
                # would drop the edge -- and Menhir's own `_RELATION_COMPLETENESS_CORE`
                # tells the model to include `user` in extracted_entities, which puts the endpoint
                # in `known` and silently disabled the whole policy. Break: a doomed edge must not
                # go on to mint a synthesized endpoint entity for its other side.
                edge_is_self_echo = True
                break
            if norm_key in known:
                continue
            marker = _active_subject_marker(receipt)
            if marker and endpoint_name == marker:
                # The marker is grounded by the receipt, not by user text.  Materialize it only
                # when the extractor used it as an endpoint. This carrier is only transport and
                # is later replaced with Menhir's preallocated author; it has no identity authority.
                entities.append({"name": marker, "entity_type_id": -1})
                known.add(norm_key)
                synthesized += 1
                continue
            if _is_unresolved_self_like_endpoint(norm_key, episode_text):
                # Normalize the endpoint spelling and materialize it ONCE per payload so Graphiti
                # does not drop the edge. This is availability recovery, not identity resolution:
                # the node remains an ordinary candidate unless a separate structured producer
                # declares its exact UUID after extraction.
                edge[endpoint_key] = _SELF_ENTITY_NAME
                if self_key not in known:
                    entities.append({"name": _SELF_ENTITY_NAME, "entity_type_id": -1})
                    known.add(self_key)
                self_like_endpoints_retained += 1
                continue
            previous_episode_texts = (
                (
                    *receipt.previous_episode_texts,
                    *receipt.relationless_repair_context_texts,
                )
                if receipt is not None
                else ()
            )
            if _is_synthesizable_endpoint(
                endpoint_name,
                episode_text,
                previous_episode_texts,
            ):
                entities.append({"name": endpoint_name.strip(), "entity_type_id": -1})
                known.add(norm_key)
                synthesized += 1
            # Otherwise leave it missing. NOTE: graphiti drops this one edge during resolution --
            # true locally, but if it was the LAST edge every node it would have connected is then
            # orphan-pruned and the whole episode collapses. The self-like case above is retained
            # only to avoid that cascade; it is deliberately not promoted to canonical self.
        if edge_is_self_echo:
            self_echo_edges += 1
            continue
        surviving_edges.append(edge)

    # Echo edges are dropped EXPLICITLY rather than by leaving an endpoint unbound. The old
    # implicit enforcement only worked when the model omitted `user` from extracted_entities;
    # when it lists `user` -- which the extraction prompt asks it to do -- the endpoint resolves
    # and the echo edge survived, duplicating the user's own first-hand fact under the assistant's
    # paraphrase while the receipt reported zero suppressed.
    # The titled-list fallback below keeps its ORIGINAL trigger: it asks whether the extractor
    # produced any edge at all, which is the question it was written to ask. Testing the post-drop
    # list instead would newly fire list synthesis on echo-only assistant turns -- a separate
    # decision that this fix deliberately does not make.
    extractor_produced_edges = bool(edges)
    edges = surviving_edges

    # Titled list: the turn states membership through SYNTAX rather than a verb, so the extractor
    # returns names with no relation between them. Every node is then orphan-pruned for want of an
    # edge and the whole episode collapses -- the content is correct and is lost anyway. Emit the
    # membership the list states, which keeps the names connected and makes the collapse moot.
    # Only when the extractor found NO usable edges: if it did state relations, they are the truth
    # of the turn and a synthetic membership edge must not compete with them.
    list_edges_added = 0
    if not extractor_produced_edges:
        # Imported lazily: `parse_titled_list` stays on the facade module (its source is pinned
        # there by the CF-193 drift guard), which imports this module at import time.
        from menhir.infrastructure.graphiti_extraction_patches import parse_titled_list

        parsed = parse_titled_list(episode_text)
        if parsed is not None:
            container, items = parsed
            container_key = _normalize_endpoint_name(container)
            extracted_keys = {_normalize_endpoint_name(e["name"]) for e in entities}
            # Require the extractor to have independently seen the items. The parse decides they are
            # a LIST; the extractor decides they are ENTITIES. Needing both means a mis-parse of
            # prose cannot mint nodes on its own.
            matched = [it for it in items if _normalize_endpoint_name(it) in extracted_keys]
            if len(matched) >= 3:
                if container_key not in extracted_keys:
                    entities.append({"name": container, "entity_type_id": -1})
                    extracted_keys.add(container_key)
                for item in matched:
                    # Built through the SAME sanitizer every model-produced edge goes through, so a
                    # synthetic edge can never carry a shape the real path would have rejected or
                    # normalized differently (e.g. `episode_indices`, which graphiti uses to map the
                    # edge to its source episode and to pick its reference time).
                    synthetic = _sanitize_combined_edge({
                        "relation_type": _MEMBERSHIP_RELATION,
                        "source_entity_name": item,
                        "target_entity_name": container,
                        # Menhir built this fact, not the model. It carries the synthetic marker so
                        # the storage boundary classifies it honestly instead of stamping
                        # fact_source="original" on a sentence the model never asserted.
                        "fact": f"{SYNTHETIC_FACT_PREFIX}{item} is listed under {container}",
                        "episode_indices": [0],
                    })
                    if synthetic is None:      # unreachable today; fail closed rather than emit junk
                        continue
                    edges.append(synthetic)
                    list_edges_added += 1

    if receipt is not None:
        receipt.malformed_entities_dropped = entities_dropped
        receipt.malformed_edges_dropped = edges_dropped
        receipt.endpoints_synthesized = synthesized
        receipt.self_echo_edges_suppressed = self_echo_edges
        receipt.list_membership_edges_added = list_edges_added
        receipt.context_unsupported_edges_suppressed = context_unsupported_edges
        receipt.subject_marker_edges_suppressed = subject_marker_edges_suppressed
        receipt.assistant_self_only_relationless = assistant_self_only_relationless
        # The validator runs once per extraction call and cannot see which pass it is in, so the
        # repair flag -- set by `_run_graphiti_combined_extraction` BEFORE the second call -- is the
        # discriminator. Writing both passes into one field would let the repair's shape overwrite
        # the first pass's evidence, which is exactly what `is_policy_empty_extraction` must not
        # lose sight of.
        if receipt.relationless_repair_attempted:
            receipt.repair_self_only_entities = _all_self_labels
        else:
            receipt.initial_self_only_entities = _all_self_labels

    if (
        entities_dropped
        or edges_dropped
        or synthesized
        or self_like_endpoints_retained
        or self_echo_edges
        or list_edges_added
        or context_unsupported_edges
    ):
        logger.info(
            "Combined-extraction sanitation: entities_dropped=%d edges_dropped=%d "
            "endpoints_synthesized=%d self_like_endpoints_retained=%d "
            "self_echo_edges_suppressed=%d "
            "list_membership_edges_added=%d context_unsupported_edges_suppressed=%d "
            "(raw entities=%d edges=%d)",
            entities_dropped,
            edges_dropped,
            synthesized,
            self_like_endpoints_retained,
            self_echo_edges,
            list_edges_added,
            context_unsupported_edges,
            len(raw_entities),
            len(raw_edges),
        )

    data["extracted_entities"] = entities
    data["edges"] = edges
    return data
