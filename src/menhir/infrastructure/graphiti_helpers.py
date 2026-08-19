"""Pure utility helpers for Graphiti JSON parsing and diagnostics."""

from __future__ import annotations

import json
import re
from typing import Any

SYNTHETIC_FACT_PREFIX = "[synthetic] "


def _extract_first_json_payload(raw: str) -> str:
    """Return the first decodable JSON object/array from a model response.

    A fenced code block is preferred when present, regardless of any preamble
    before it. Otherwise the text is scanned from left to right: at each offset
    holding a ``{`` or ``[`` a raw JSON decode is attempted, and the first
    payload that decodes cleanly is returned.
    """

    text = raw.strip()
    if not text:
        raise ValueError(
            "You returned an empty response. You MUST respond with a valid JSON object. "
            "Do not return blank/empty output. Produce the requested JSON now."
        )

    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    decoder = json.JSONDecoder()
    for offset, char in enumerate(text):
        if char not in {"{", "["}:
            continue
        try:
            _, consumed = decoder.raw_decode(text, offset)
        except ValueError:  # JSONDecodeError subclasses ValueError
            continue
        return text[offset : offset + consumed]
    return text


def _raw_preview(raw: str, limit: int = 500) -> str:
    """Return a single-line preview of raw model output for diagnostics."""

    rendered = " ".join(str(raw or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _messages_preview(messages: list[dict[str, str]], limit: int = 1200) -> str:
    """Render a compact prompt preview for failure diagnostics."""

    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown").strip()
        content = _raw_preview(str(message.get("content") or ""), limit=240)
        parts.append(f"{role}: {content}")
    preview = " | ".join(parts)
    if len(preview) <= limit:
        return preview
    return preview[: limit - 3] + "..."


def _describe_openai_client_base_url(client: Any) -> str:
    """Resolve a readable base URL for OpenAI-compatible client objects."""

    if client is None:
        return "unknown"
    for attr in ("base_url", "_base_url", "base_url_url"):
        raw = getattr(client, attr, None)
        if raw:
            return str(raw)
    return str(client.__class__.__name__)


def _build_graphiti_edge_fact(edge_payload: dict[str, Any]) -> tuple[str, bool] | None:
    """Derive a usable edge fact when the model omits Graphiti's required field.

    Returns ``(fact_text, is_synthetic)`` or None if no fact can be derived.

    ``fact`` and ``relationship`` are treated as MODEL-ASSERTED: both name the edge's own
    relationship text, and ``relationship`` is the key-variant spelling local models emit for it
    (see ``test_normalize_graphiti_json_payload_prefers_alternate_edge_text_before_synthesizing``,
    which defines that tolerance).

    ``description`` and ``summary`` are BORROWED: they describe a thing, not the relation between
    two things, so promoting one into an edge's ``fact`` asserts as the relation's fact something
    the model stated about a node. Those return synthetic so the storage boundary stamps
    ``fact_source`` honestly instead of "original", and so the existing repair path gets a chance
    to produce a real relation fact.
    """

    _MODEL_ASSERTED_FACT_KEYS = ("fact", "relationship")
    for candidate_key in ("fact", "relationship", "description", "summary"):
        candidate = str(edge_payload.get(candidate_key) or "").strip()
        if candidate:
            return candidate, candidate_key not in _MODEL_ASSERTED_FACT_KEYS

    source = str(edge_payload.get("source_entity_name") or "").strip()
    target = str(edge_payload.get("target_entity_name") or "").strip()
    relation_type = str(edge_payload.get("relation_type") or "").strip()
    if not source or not target or not relation_type:
        return None

    relation_phrase = relation_type.replace("_", " ").strip().lower()
    if not relation_phrase:
        return None
    return f"{source} {relation_phrase} {target}", True


def _normalize_graphiti_json_payload(payload: Any) -> Any:
    """Apply lightweight key normalization before Graphiti validation."""

    if isinstance(payload, dict):
        normalized: dict[str, Any] = {}
        resolved_name = payload.get("name") or payload.get("entity_name") or payload.get("entity")
        for key, value in payload.items():
            if key in {"entity_name", "entity"}:
                continue
            new_key = key
            if key == "type" and "entity_type_id" not in payload:
                new_key = "entity_type_id"
            normalized[new_key] = _normalize_graphiti_json_payload(value)
        if resolved_name:
            normalized["name"] = _normalize_graphiti_json_payload(resolved_name)
        if (
            "fact" not in normalized
            and "source_entity_name" in normalized
            and "target_entity_name" in normalized
            and "relation_type" in normalized
        ):
            fact_result = _build_graphiti_edge_fact(normalized)
            if fact_result is not None:
                fact_text, is_synthetic = fact_result
                normalized["fact"] = (SYNTHETIC_FACT_PREFIX + fact_text) if is_synthetic else fact_text
        # Ensure critical string fields are never None — Graphiti calls
        # .replace() on these and crashes with NoneType if they're null.
        for str_field in ("name", "fact"):
            if str_field in normalized and normalized[str_field] is None:
                normalized[str_field] = ""
        return normalized
    if isinstance(payload, list):
        return [_normalize_graphiti_json_payload(item) for item in payload]
    return payload


def _build_graphiti_failure_details(
    *,
    model: str,
    endpoint: str,
    messages: list[dict[str, str]],
    raw_result: str,
    response_status: Any,
    response_id: Any,
) -> dict[str, Any]:
    """Capture the high-signal request/response context for parse failures."""

    return {
        "graphiti_model": model,
        "graphiti_endpoint": endpoint,
        "graphiti_message_count": len(messages),
        "graphiti_prompt_preview": _messages_preview(messages),
        "graphiti_raw_response_preview": _raw_preview(raw_result, limit=1200),
        "graphiti_raw_response_length": len(str(raw_result or "")),
        "graphiti_response_status": response_status,
        "graphiti_response_id": response_id,
    }


def strip_synthetic_prefix(fact: str) -> str:
    """Remove the synthetic marker prefix if present."""
    if fact.startswith(SYNTHETIC_FACT_PREFIX):
        return fact[len(SYNTHETIC_FACT_PREFIX):]
    return fact
