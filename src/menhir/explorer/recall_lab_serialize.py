"""Serialization and redaction of Recall Lab arm results."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from menhir.privacy import MASK, redact_mapping, redact_text


def _value(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _mapping(value: object | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if is_dataclass(value):
        raw = asdict(value)
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        return None
    return {key: _enum_value(item) for key, item in raw.items()}


def _trace_row(trace: object | None, uuid: str) -> object | None:
    for row in _value(trace, "candidates", []) or []:
        if str(_value(row, "uuid", "")) == uuid:
            return row
    return None


def _redact_authority_verdicts(verdicts: list[dict[str, Any]], *, reveal: bool) -> list[dict[str, Any]]:
    """Mask the free-text fields ScalarAuthorityVerdict/EventAuthorityVerdict carry.

    ``value`` and each contributor's ``stated_span`` (a quoted evidence excerpt) are memory
    content, not structural metadata -- neither is in privacy.REDACTED_FIELDS (that vocabulary
    predates this authority-layer shape), so redact_mapping's flat field-name match can't cover
    them. Everything else (kind/status/subject_uuid/attribute/scope/valid_at/view_uuid/
    has_foundation/predicate/object_key/...) is structural and passes through unchanged.

    ``value`` is masked unconditionally (not via redact_text) because it is often non-string
    (a spend amount, a duration, a count) -- redact_text only masks strings, so routing a
    numeric authority value through it would silently leak the exact figure in redacted mode.
    """
    if reveal:
        return verdicts
    out: list[dict[str, Any]] = []
    for verdict in verdicts:
        v = dict(verdict)
        if "value" in v and v["value"] not in (None, ""):
            v["value"] = MASK
        if "object_display" in v:
            v["object_display"] = redact_text(v["object_display"], reveal=False)
        # EventAuthorityVerdict carries its own top-level stated_span (no contributors list);
        # ScalarAuthorityVerdict's is nested per-contributor below. Cover both shapes.
        if "stated_span" in v:
            v["stated_span"] = redact_text(v["stated_span"], reveal=False)
        contributors = v.get("contributors")
        # asdict() preserves tuple-typed fields as tuples, not lists (ScalarAuthorityVerdict
        # declares contributors: tuple[...]) -- check both, and always emit a list back out.
        if isinstance(contributors, (list, tuple)):
            v["contributors"] = [
                {**c, "stated_span": redact_text(c.get("stated_span"), reveal=False)}
                if isinstance(c, dict) else c
                for c in contributors
            ]
        out.append(v)
    return out


def _redact_temporal_facts(facts: list[dict[str, Any]], *, reveal: bool) -> list[dict[str, Any]]:
    """Mask ``fact`` (the free-text bi-temporal fact string) on each TemporalFact dict.

    Same gap as authority_layer: ``fact`` is memory content, not in privacy.REDACTED_FIELDS
    (that vocabulary predates this shape), so redact_mapping's flat field-name match can't
    reach it. valid_at/invalid_at/created_at/expired_at/is_current_belief/temporal_role are
    structural (dates and an enum-like role label) and pass through unchanged.
    """
    if reveal:
        return facts
    out: list[dict[str, Any]] = []
    for fact in facts:
        f = dict(fact)
        if "fact" in f:
            f["fact"] = redact_text(f["fact"], reveal=False)
        out.append(f)
    return out


def _serialize_result(result: object, *, reveal: bool) -> dict[str, Any]:
    trace = _value(result, "trace")
    memories: list[dict[str, Any]] = []
    for rank, memory in enumerate(_value(result, "results", []) or [], start=1):
        uuid = str(_value(memory, "uuid", ""))
        candidate = _trace_row(trace, uuid)
        source = _enum_value(_value(candidate, "source"))
        contributors = [
            str(_enum_value(item))
            for item in sorted(
                _value(candidate, "contributing_sources", frozenset()) or [],
                key=lambda item: str(_enum_value(item)),
            )
        ]
        row = {
            "rank": rank,
            "uuid": uuid,
            "name": str(_value(memory, "name", "")),
            "content": _value(memory, "content"),
            "scope": str(_value(memory, "scope", "")),
            "memory_type": str(_value(memory, "memory_type", "")),
            "final_score": float(_value(memory, "final_score", 0.0) or 0.0),
            "retrieval_score": _value(memory, "retrieval_score"),
            "retrieval_score_kind": str(
                _enum_value(_value(memory, "retrieval_score_kind", "graphiti_rrf"))
            ),
            "source": source,
            "contributing_sources": contributors,
            "similarity": _value(candidate, "similarity"),
            "bm25_rank": _value(candidate, "bm25_rank"),
            "cosine_rank": _value(candidate, "cosine_rank"),
            "content_rank": _value(candidate, "content_rank"),
            "content_cosine": _value(candidate, "content_cosine"),
            "warden_label": _enum_value(_value(memory, "warden_label")),
            "score_parts": _mapping(_value(candidate, "score_parts")),
            # Recall Lab arms bypass the /api/recall route's authority formatting; surface these
            # two flags directly off ScoredMemory so a caller can reconstruct the same
            # [AUTHORITATIVE CURRENT MEMORY] / [SUPERSEDED ... MEMORY] labeling the production
            # endpoint provides (needed for apples-to-apples benchmark comparisons).
            "is_superseded_view": bool(_value(memory, "is_superseded_view", False)),
            "is_scalar_authority": bool(_value(memory, "is_scalar_authority", False)),
            # Also dropped like authority_layer: /api/recall (routes.py) includes each
            # memory's temporal_facts (world-time/belief-time/supersession evidence);
            # Recall Lab silently omitted it, so any "previously vs now" / supersession
            # reasoning the answer prompt depends on this for was uniformly unavailable
            # across every Recall Lab arm regardless of tuning.
            "temporal_facts": _redact_temporal_facts(
                [asdict(t) for t in _value(memory, "temporal_facts", ()) or ()],
                reveal=reveal,
            ),
        }
        memories.append(redact_mapping(row, reveal=reveal))

    # Mirrors api/routes.py's RecallResponse.authority_layer/event_authority_layer: dataclass
    # field names already match ScalarAuthorityVerdictResponse/EventAuthorityVerdictResponse,
    # so asdict() round-trips cleanly through the same JSON shape /api/recall emits.
    authority_layer = _value(result, "authority_layer")
    event_authority_layer = _value(result, "event_authority_layer")

    return {
        "query": str(_value(result, "query", "")),
        "preset": str(_enum_value(_value(result, "preset", ""))),
        "results": memories,
        "candidates_evaluated": int(_value(result, "candidates_evaluated", 0) or 0),
        "nodes_touched": int(_value(result, "nodes_touched", 0) or 0),
        "note": _value(result, "note"),
        "search_error": _value(result, "search_error"),
        "service_ms": _value(trace, "total_ms"),
        "phases": dict(_value(trace, "phases", {}) or {}),
        "rank_shadow_warning": _value(trace, "rank_shadow_warning"),
        "authority_layer": (
            _redact_authority_verdicts([asdict(v) for v in authority_layer], reveal=reveal)
            if authority_layer else None
        ),
        "event_authority_layer": (
            _redact_authority_verdicts([asdict(v) for v in event_authority_layer], reveal=reveal)
            if event_authority_layer else None
        ),
    }
