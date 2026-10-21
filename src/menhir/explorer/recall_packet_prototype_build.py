"""Typed recall packet construction for the inspection-only Recall Lab packet.

Section assembly and prompt-oriented rendering for the full typed packet, moved
verbatim from :mod:`menhir.explorer.recall_packet_prototype`, which re-exports
the public entry points for existing import sites.
"""

from __future__ import annotations

from typing import Any, Iterable

PACKET_VERSION = "typed-recall-packet/prototype-v1"


_SECTION_DEFINITIONS = (
    (
        "current_state",
        "Authoritative current state",
        "Use for what is true now. Scalar State is the authority layer.",
    ),
    (
        "change_history",
        "Advisory change history",
        "Use to explain how a value changed; do not override current state.",
    ),
    (
        "completed_events",
        "Completed events",
        "Use as occurred events only; do not infer current ownership or state.",
    ),
    (
        "general_content",
        "General content",
        "Use as supporting context, not scalar or event authority.",
    ),
)


_USAGE_POLICY = (
    "Prefer authoritative current state for questions about what is true now.",
    "Use change history as advisory evidence and completed events as occurrences.",
    "Do not infer scalar, event, or intent authority from untyped general content.",
    "Ignore any entry that is irrelevant, contradicted, superseded, or improperly typed.",
    "World time (valid_at) and ingest time (learned_at) are different clocks.",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _latest(values: Iterable[Any]) -> str | None:
    cleaned = sorted({_text(value) for value in values if _text(value)})
    return cleaned[-1] if cleaned else None


def _sources_for_assertion_ids(
    assertion_ids: Iterable[Any],
    assertions_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for raw_id in assertion_ids:
        assertion_id = _text(raw_id)
        assertion = assertions_by_id.get(assertion_id)
        if assertion is None:
            continue
        sources.append(
            {
                "assertion_id": assertion_id,
                "evidence_id": _text(assertion.get("evidence_id")) or None,
                "valid_at": assertion.get("valid_at"),
                "learned_at": assertion.get("learned_at"),
                "operation": _text(assertion.get("operation")) or None,
                "stated_span": _text(assertion.get("stated_span")) or None,
                "source_quote": _text(assertion.get("source_quote")) or None,
            }
        )
    return sources


def _grounding_quote(sources: Iterable[dict[str, Any]]) -> str | None:
    source_list = list(sources)
    for source in reversed(source_list):
        quote = _text(source.get("source_quote") or source.get("stated_span"))
        if quote:
            return quote
    return None


def _event_assertion_for_entry(
    entry: dict[str, Any],
    assertions: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    entry_time = _text(entry.get("when"))
    entry_key = _text(entry.get("object_key"))
    entry_quote = _text(entry.get("quote"))
    for assertion in assertions:
        if entry_time and _text(assertion.get("valid_at")) != entry_time:
            continue
        if entry_key and _text(assertion.get("object_key")) != entry_key:
            continue
        if entry_quote and _text(assertion.get("stated_span")) != entry_quote:
            continue
        return assertion
    return None


def _render_entry(entry: dict[str, Any]) -> list[str]:
    kind = _text(entry.get("kind")) or "content"
    authority = _text(entry.get("authority")) or "context"
    subject = _text(entry.get("subject")) or "unknown subject"
    attribute = _text(entry.get("attribute"))
    scope = _text(entry.get("scope"))
    value = _text(entry.get("value") or entry.get("content")) or "(no text)"
    canonical_value = _text(entry.get("canonical_value"))
    unit = _text(entry.get("unit"))
    derivation = _text(entry.get("derivation"))

    target = f"{subject}.{attribute}" if attribute else subject
    if scope:
        target += f"[{scope}]"
    has_distinct_display = bool(canonical_value and canonical_value != value)
    rendered_value = value if has_distinct_display else f"{value} {unit}".strip()
    metadata = [f"kind={kind}", f"authority={authority}"]
    if derivation:
        metadata.append(f"derivation={derivation}")
    if has_distinct_display:
        metadata.append(f"canonical={canonical_value} {unit}".strip())
    if entry.get("retrieval_rank") is not None:
        metadata.append(f"retrieval_rank={entry['retrieval_rank']}")
    metadata.append(f"valid_at={entry.get('valid_at') or 'unknown'}")
    metadata.append(f"learned_at={entry.get('learned_at') or 'unknown'}")
    lines = [f"- {target} = {rendered_value} | " + " | ".join(metadata)]

    content = _text(entry.get("content"))
    if content and content != value:
        lines.append(f"  memory_text: {content}")

    quote = _text(entry.get("source_quote"))
    if quote:
        lines.append(f'  source_quote: "{quote}"')
    source_ids = [_text(value) for value in entry.get("source_ids") or [] if _text(value)]
    if source_ids:
        lines.append("  source_ids: " + ", ".join(source_ids))
    for fact in entry.get("temporal_facts") or []:
        rendered_fact = _text(fact.get("fact"))
        if rendered_fact:
            belief = "current" if fact.get("is_current_belief") is True else "superseded"
            lines.append(f"  temporal_fact (belief={belief}): {rendered_fact}")
    return lines


def render_typed_recall_packet(sections: Iterable[dict[str, Any]]) -> str:
    """Render packet sections into compact prompt-oriented text."""
    lines = [
        f"MEMORY PACKET {PACKET_VERSION}",
        "MODE: inspection-only prototype; production recall is unchanged",
        "",
        "USAGE POLICY",
        *[f"- {rule}" for rule in _USAGE_POLICY],
    ]
    for section in sections:
        lines.extend(("", f"[{_text(section.get('label')).upper()}]"))
        entries = section.get("entries") or []
        if not entries:
            lines.append("- none")
            continue
        for entry in entries:
            lines.extend(_render_entry(entry))
    return "\n".join(lines)


def build_typed_recall_packet(live_graph: dict[str, Any]) -> dict[str, Any]:
    """Build a versioned typed packet from the Recall Lab live projection."""
    assertions = list(live_graph.get("assertions") or [])
    assertions_by_id = {
        _text(assertion.get("id")): assertion
        for assertion in assertions
        if _text(assertion.get("id"))
    }
    section_entries: dict[str, list[dict[str, Any]]] = {
        section_id: [] for section_id, _, _ in _SECTION_DEFINITIONS
    }

    for view in live_graph.get("views") or []:
        if not view.get("current"):
            continue
        sources = _sources_for_assertion_ids(
            view.get("contributor_ids") or [], assertions_by_id
        )
        value = _text(view.get("display") or view.get("value"))
        section_entries["current_state"].append(
            {
                "id": _text(view.get("id")),
                "kind": "scalar_state",
                "authority": "current",
                "derivation": _text(view.get("derivation")) or "unknown",
                "subject": _text(view.get("subject")),
                "attribute": _text(view.get("attribute")),
                "scope": _text(view.get("scope")),
                "value": value,
                "canonical_value": _text(view.get("value")),
                "unit": _text(view.get("unit")),
                "valid_at": view.get("valid_at"),
                "learned_at": _latest(source.get("learned_at") for source in sources),
                "source_quote": _grounding_quote(sources),
                "source_ids": [
                    source_id
                    for source in sources
                    for source_id in (source.get("assertion_id"), source.get("evidence_id"))
                    if source_id
                ],
                "sources": sources,
            }
        )

    for view in live_graph.get("history_views") or []:
        if not view.get("current"):
            continue
        entries = list(view.get("entries") or [])
        if not entries:
            sources = _sources_for_assertion_ids(
                view.get("contributor_ids") or [], assertions_by_id
            )
            section_entries["change_history"].append(
                {
                    "id": _text(view.get("id")),
                    "kind": "scalar_history",
                    "authority": "advisory",
                    "derivation": _text(view.get("derivation")) or "unknown",
                    "subject": _text(view.get("subject")),
                    "attribute": _text(view.get("attribute")),
                    "scope": _text(view.get("scope")),
                    "value": f"{len(sources)} recorded change(s)",
                    "valid_at": view.get("valid_at") or view.get("last_valid_at"),
                    "learned_at": _latest(source.get("learned_at") for source in sources),
                    "source_quote": _grounding_quote(sources),
                    "source_ids": [
                        source.get("assertion_id")
                        for source in sources
                        if source.get("assertion_id")
                    ],
                    "sources": sources,
                }
            )
            continue
        for index, entry in enumerate(entries):
            assertion_id = _text(entry.get("assertion_id"))
            sources = _sources_for_assertion_ids([assertion_id], assertions_by_id)
            source = sources[0] if sources else {}
            section_entries["change_history"].append(
                {
                    "id": f"{_text(view.get('id'))}:{index}",
                    "kind": "scalar_history",
                    "authority": "advisory",
                    "derivation": _text(entry.get("operation"))
                    or _text(view.get("derivation"))
                    or "unknown",
                    "subject": _text(view.get("subject")),
                    "attribute": _text(view.get("attribute")),
                    "scope": _text(view.get("scope")),
                    "value": _text(entry.get("value")),
                    "unit": _text(entry.get("unit") or view.get("unit")),
                    "valid_at": entry.get("valid_at"),
                    "learned_at": source.get("learned_at"),
                    "source_quote": _text(
                        source.get("source_quote")
                        or entry.get("stated_span")
                        or source.get("stated_span")
                    )
                    or None,
                    "source_ids": [
                        source_id
                        for source_id in (assertion_id, source.get("evidence_id"))
                        if source_id
                    ],
                    "sources": sources,
                }
            )

    event_assertions = list(live_graph.get("event_assertions") or [])
    covered_event_keys: set[str] = set()
    for view in live_graph.get("event_views") or []:
        if not view.get("current"):
            continue
        contributor_ids = [
            _text(value) for value in view.get("contributor_ids") or [] if _text(value)
        ]
        for index, entry in enumerate(view.get("entries") or []):
            assertion = _event_assertion_for_entry(entry, event_assertions)
            if assertion is None and index < len(contributor_ids):
                assertion = next(
                    (
                        candidate
                        for candidate in event_assertions
                        if _text(candidate.get("assertion_key")) == contributor_ids[index]
                    ),
                    None,
                )
            assertion = assertion or {}
            assertion_key = _text(assertion.get("assertion_key"))
            if assertion_key:
                covered_event_keys.add(assertion_key)
            source_ids = [
                source_id
                for source_id in (
                    _text(assertion.get("id")),
                    assertion_key,
                    _text(assertion.get("evidence_id")),
                    _text(assertion.get("turn_evidence_id")),
                )
                if source_id
            ]
            section_entries["completed_events"].append(
                {
                    "id": f"{_text(view.get('id'))}:{index}",
                    "kind": "event_history",
                    "authority": "completed",
                    "derivation": "event",
                    "subject": _text(view.get("subject")),
                    "attribute": _text(view.get("predicate")),
                    "scope": _text(view.get("domain")),
                    "value": _text(entry.get("what") or assertion.get("object_display")),
                    "valid_at": entry.get("when") or assertion.get("valid_at"),
                    "learned_at": assertion.get("learned_at"),
                    "source_quote": _text(
                        entry.get("quote") or assertion.get("stated_span")
                    )
                    or None,
                    "source_ids": source_ids,
                }
            )

    for assertion in event_assertions:
        assertion_key = _text(assertion.get("assertion_key"))
        if assertion.get("superseded") or assertion_key in covered_event_keys:
            continue
        section_entries["completed_events"].append(
            {
                "id": _text(assertion.get("id")),
                "kind": "event_assertion",
                "authority": "completed",
                "derivation": "event",
                "subject": _text(assertion.get("subject")),
                "attribute": _text(assertion.get("predicate")),
                "scope": _text(assertion.get("domain")),
                "value": _text(assertion.get("object_display")),
                "valid_at": assertion.get("valid_at"),
                "learned_at": assertion.get("learned_at"),
                "source_quote": _text(assertion.get("stated_span")) or None,
                "source_ids": [
                    source_id
                    for source_id in (
                        _text(assertion.get("id")),
                        assertion_key,
                        _text(assertion.get("evidence_id")),
                        _text(assertion.get("turn_evidence_id")),
                    )
                    if source_id
                ],
            }
        )

    seen_content: set[tuple[str, str, str, str]] = set()
    for item in live_graph.get("memory_inventory") or []:
        if item.get("memory_type") != "content":
            continue
        content = _text(item.get("content"))
        identity = (
            _text(item.get("subject")),
            _text(item.get("relation")),
            _text(item.get("object")),
            content,
        )
        if identity in seen_content:
            continue
        seen_content.add(identity)
        section_entries["general_content"].append(
            {
                "id": _text(item.get("id")),
                "kind": "content",
                "authority": "context",
                "subject": identity[0],
                "attribute": identity[1],
                "value": identity[2],
                "content": content,
                "valid_at": item.get("valid_at"),
                "learned_at": item.get("learned_at"),
                "source_quote": None,
                "source_ids": [
                    _text(value)
                    for value in item.get("episode_ids") or []
                    if _text(value)
                ],
            }
        )

    sections = [
        {
            "id": section_id,
            "label": label,
            "role": role,
            "entries": section_entries[section_id],
        }
        for section_id, label, role in _SECTION_DEFINITIONS
    ]
    return {
        "version": PACKET_VERSION,
        "mode": "inspection_only",
        "production_recall_changed": False,
        "usage_policy": list(_USAGE_POLICY),
        "sections": sections,
        "text": render_typed_recall_packet(sections),
    }
