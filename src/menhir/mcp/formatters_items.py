"""MCP formatter item serializers — compact JSON and recall item rendering."""

from __future__ import annotations

import json

from menhir.domain.utils import excerpt
from menhir.services.stale_labeling import (
    STALE_ACTION, STALE_ACTION_OUTDATED,
    STALE_ADVISORY, STALE_ADVISORY_OUTDATED, STALE_ADVISORY_STILL_VALID,
)


def _compact_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str)


def _compact_memory_item(row: dict[str, object], *, tag: str) -> dict[str, object]:
    item = {
        "uuid": row.get("uuid"),
        "name": row.get("name") or row.get("uuid") or "(unnamed)",
        "scope": row.get("scope") or "UNKNOWN",
        "type": row.get("type") or "UNKNOWN",
        "tag": tag,
        "summary": excerpt(row.get("summary") or row.get("content")),
    }
    if row.get("similarity") is not None:
        item["similarity"] = row.get("similarity")
    # Stale-anchor advisory: pass through when the recall service labeled this item.
    stale_info = row.get("stale_anchor_info")
    if stale_info is not None:
        item["stale_anchor"] = stale_info.get("stale_anchor", False)
        if stale_info.get("stale_anchor"):
            item["stale_reason"] = stale_info.get("stale_reason")
            item["dirty_at"] = stale_info.get("dirty_at")
            item["anchored_at"] = stale_info.get("anchored_at")
            item["path"] = stale_info.get("path")
            action, advisory = _resolve_stale_advisory(stale_info)
            item["stale_action"] = action
            item["stale_advisory"] = advisory
            verification = stale_info.get("stale_verification")
            if verification:
                item["stale_verification"] = verification
    return item


def _bd(breakdown: object | None, key: str) -> float:
    """Extract a breakdown value that may be a dict (from JSON round-trip) or a dataclass."""
    if breakdown is None:
        return 0.0
    if isinstance(breakdown, dict):
        return float(breakdown.get(key, 0.0) or 0.0)
    return float(getattr(breakdown, key, 0.0) or 0.0)


def _tf(fact: object, key: str) -> object:
    """Extract a temporal-fact value that may be a dict (post-asdict JSON round-trip) or a dataclass.

    The MCP recall path serializes RecallResult via asdict() before this formatter
    runs, so each temporal fact arrives as a plain dict; direct paths may pass the
    TemporalFact dataclass. Mirror _bd and tolerate both.
    """
    if isinstance(fact, dict):
        return fact.get(key)
    return getattr(fact, key, None)


def _resolve_stale_advisory(stale_info: dict[str, object]) -> tuple[str, str]:
    """Return (action, advisory) for a stale item, adjusted by verification outcome."""
    action: str = STALE_ACTION
    advisory: str = STALE_ADVISORY
    verification = stale_info.get("stale_verification")
    if verification and isinstance(verification, dict):
        outcome = str(verification.get("outcome") or "")
        if outcome == "still_valid":
            advisory = STALE_ADVISORY_STILL_VALID
        elif outcome == "outdated":
            action = STALE_ACTION_OUTDATED
            advisory = STALE_ADVISORY_OUTDATED
    return action, advisory


def _compact_scored_item(scored: object, compact: bool = False) -> dict[str, object]:
    breakdown = getattr(scored, "breakdown", None)
    sim_raw = _bd(breakdown, "semantic_similarity")
    relevance = "high" if sim_raw >= 0.7 else "medium" if sim_raw >= 0.4 else "low"
    item: dict[str, object] = {
        "uuid": getattr(scored, "uuid", None),
        "name": getattr(scored, "name", None),
        "scope": getattr(scored, "scope", None),
        "score": round(float(getattr(scored, "final_score", 0.0) or 0.0), 3),
        "relevance": relevance,
        "summary": excerpt(getattr(scored, "summary", None) or getattr(scored, "content", None)),
        "retrieval_score": round(
            float(getattr(scored, "retrieval_score", sim_raw) or 0.0), 6
        ),
        "retrieval_score_kind": getattr(
            getattr(scored, "retrieval_score_kind", "graphiti_rrf"),
            "value",
            getattr(scored, "retrieval_score_kind", "graphiti_rrf"),
        ),
        "relevance_basis": "legacy_rrf_threshold_unvalidated",
    }
    # Frontier warden gate: a FLAG label (historical/conflict/uncertain) is decision-relevant,
    # so it is kept even in compact mode. Absent on the old path (None) -> omitted.
    warden_label = getattr(scored, "warden_label", None)
    if warden_label:
        item["warden_label"] = warden_label
    # Phase 4a.4/4c: the deterministically-injected current scalar_state View is the authoritative
    # CURRENT value for a surfaced slot. Marked so the consumer leads with it (the other observations
    # are its history/provenance). Decision-relevant -> kept even in compact mode.
    if getattr(scored, "is_scalar_authority", False):
        item["is_scalar_authority"] = True
    # Hook Center stale-anchor metadata: present only when the recall service
    # performed stale labeling (stale_anchor_info is a dict). Always includes
    # "stale_anchor": true|false. Additional fields for stale items:
    # stale_reason, dirty_at, anchored_at, path.
    stale_info = getattr(scored, "stale_anchor_info", None)
    if stale_info is not None:
        item["stale_anchor"] = stale_info.get("stale_anchor", False)
        if stale_info.get("stale_anchor"):
            item["stale_reason"] = stale_info.get("stale_reason")
            item["dirty_at"] = stale_info.get("dirty_at")
            item["anchored_at"] = stale_info.get("anchored_at")
            item["path"] = stale_info.get("path")
            action, advisory = _resolve_stale_advisory(stale_info)
            item["stale_action"] = action
            item["stale_advisory"] = advisory
            verification = stale_info.get("stale_verification")
            if verification:
                item["stale_verification"] = verification
    # Source/world time changes answer selection, so it is decision-relevant and survives compact
    # mode. Belief-time fields stay explicit and never stand in for an absent valid_at.
    temporal_facts_raw = getattr(scored, "temporal_facts", None) or ()
    if temporal_facts_raw:
        item["temporal_facts"] = [
            {
                "fact": _tf(tf, "fact"),
                "valid_at": _tf(tf, "valid_at"),
                "invalid_at": _tf(tf, "invalid_at"),
                "created_at": _tf(tf, "created_at"),
                "expired_at": _tf(tf, "expired_at"),
                "is_current_belief": _tf(tf, "is_current_belief"),
                "temporal_role": _tf(tf, "temporal_role"),
                # Rung 1C: render happened-time (world) vs learned-time (belief) legibly.
                "when": _format_when(tf),
            }
            for tf in temporal_facts_raw
        ]
    if compact:
        # Drop explainability/diagnostic fields the LLM does not act on. The
        # decision-relevant fields (score, scope, relevance) are retained.
        return item
    item["type"] = getattr(scored, "memory_type", None)
    item["breakdown"] = {
        "sim": round(sim_raw, 3),
        "adj": round(_bd(breakdown, "adjacency_bonus"), 3),
        "rec": round(_bd(breakdown, "recency_bonus"), 3),
        "prom": round(_bd(breakdown, "prominence_bonus"), 3),
    }
    return item


def _format_when(tf: object) -> str:
    """Rung 1C: a compact happened-vs-learned phrase for one temporal fact.

    happened = world time (valid_at..invalid_at); learned = belief time
    (created_at, and expired_at when superseded). Keeps the LLM from conflating
    'when it was true' with 'when we found out'.
    """
    valid_at = _tf(tf, "valid_at")
    invalid_at = _tf(tf, "invalid_at")
    created_at = _tf(tf, "created_at")
    expired_at = _tf(tf, "expired_at")

    if invalid_at:
        happened = f"happened {valid_at or '?'} until {invalid_at}"
    elif valid_at:
        happened = f"happened from {valid_at}"
    else:
        happened = "happened (time unstated)"

    if expired_at:
        learned = f"learned {created_at or '?'}, superseded {expired_at}"
    elif created_at:
        learned = f"learned {created_at}"
    else:
        learned = "learned (time unstated)"

    return f"{happened}; {learned}"
