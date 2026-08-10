"""Deterministic, inspection-only typed recall packet for Recall Lab.

This module intentionally lives in ``menhir.explorer``.  It formats the existing
live-graph projection for operator inspection and does not participate in
production recall, ranking, ingestion, or persistence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

PACKET_VERSION = "typed-recall-packet/prototype-v1"
QUERY_FILTERED_PACKET_VERSION = "typed-recall-packet/query-filtered-v1"
DEFAULT_QUERY_PACKET_MAX_CHARS = 6_000

_HISTORY_INTENT = re.compile(
    r"\b(?:previous(?:ly)?|earlier|formerly|before|used\s+to|"
    r"change[ds]?|history|historical|original(?:ly)?|prior|past|"
    r"then|at\s+first|how\s+many\s+more|how\s+much\s+more)\b",
    re.IGNORECASE,
)
_EVENT_INTENT = re.compile(
    r"\b(?:when|what\s+time|what\s+date|happen(?:ed|ing)?|occurred|"
    r"event|attended|visited|went|bought|purchased|completed|finished)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "can", "did", "do", "does", "for", "from", "had", "has", "have",
        "he", "her", "hers", "him", "his", "how", "i", "in", "is", "it",
        "its", "me", "my", "of", "on", "or", "our", "she", "so", "that",
        "the", "their", "them", "they", "this", "to", "was", "we", "were",
        "what", "when", "where", "which", "who", "why", "will", "with",
        "you", "your", "user", "current", "currently", "now",
    }
)

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


def _tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(_text(value).lower()):
        if token in _STOP_WORDS or len(token) < 2:
            continue
        tokens.add(token)
        if len(token) > 4 and token.endswith("s"):
            tokens.add(token[:-1])
        if len(token) > 4 and token.endswith("ed"):
            stem = token[:-2]
            tokens.add(stem)
            if stem.endswith("i"):
                tokens.add(stem[:-1] + "y")
        if len(token) > 5 and token.endswith("ing"):
            stem = token[:-3]
            tokens.add(stem)
            if len(stem) > 2 and stem[-1] == stem[-2]:
                tokens.add(stem[:-1])
    return tokens


def _entry_tokens(entry: dict[str, Any]) -> set[str]:
    return _tokens(
        " ".join(
            _text(entry.get(key))
            for key in (
                "subject", "attribute", "scope", "value", "unit", "content",
                "source_quote", "name",
            )
        )
    )


def _relevant(entry: dict[str, Any], query_tokens: set[str]) -> bool:
    return bool(query_tokens & _entry_tokens(entry))


def _identity_relevant(entry: dict[str, Any], query_tokens: set[str]) -> bool:
    """Match a typed fact by its identity, not a long shared evidence quote."""
    attribute_tokens = _tokens(entry.get("attribute"))
    scope_tokens = _tokens(entry.get("scope"))
    if scope_tokens:
        if query_tokens & scope_tokens:
            return True
        return len(query_tokens & attribute_tokens) >= 2
    if not attribute_tokens:
        return False
    # Inflection expansion makes a single lexical word such as ``owned`` yield both
    # ``owned`` and ``own``.  Threshold on source words, not expanded token variants.
    attribute_words = [
        token
        for token in _TOKEN_RE.findall(_text(entry.get("attribute")).lower())
        if token not in _STOP_WORDS and len(token) >= 2
    ]
    required = 1 if len(attribute_words) == 1 else 2
    return len(query_tokens & attribute_tokens) >= required


def _scalar_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(entry.get("subject")).lower(),
        _text(entry.get("attribute")).lower(),
        _text(entry.get("scope")).lower(),
    )


def _compact(value: Any, limit: int = 1_200) -> str:
    text = re.sub(r"\s+", " ", _text(value))
    if len(text) <= limit:
        return text
    boundary = max(text.rfind(". ", 0, limit), text.rfind("; ", 0, limit))
    if boundary < limit // 2:
        boundary = text.rfind(" ", 0, limit)
    if boundary < 1:
        boundary = limit
    return text[:boundary].rstrip(" .;") + "…"


def _query_compact(value: Any, query_tokens: set[str], limit: int = 650) -> str:
    """Compact long content around query-relevant sentences instead of its prefix."""
    text = re.sub(r"\s+", " ", _text(value))
    if len(text) <= limit:
        return text
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\s*[;|]\s*", text)
        if sentence.strip()
    ]
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (-len(query_tokens & _tokens(item[1])), item[0]),
    )
    chosen: list[tuple[int, str]] = []
    used = 0
    for index, sentence in ranked:
        sentence = _compact(sentence, limit)
        separator = 1 if chosen else 0
        if chosen and used + separator + len(sentence) > limit:
            continue
        chosen.append((index, sentence))
        used += separator + len(sentence)
        if used >= limit:
            break
    if not chosen:
        return _compact(text, limit)
    return " ".join(sentence for _, sentence in sorted(chosen))


def _section_map(packet: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        _text(section.get("id")): list(section.get("entries") or [])
        for section in packet.get("sections") or []
    }


def _temporal_fields(row: dict[str, Any]) -> tuple[Any, Any, list[dict[str, Any]]]:
    # Graph maintenance can leave metadata-only current-belief rows with no fact text.
    # They must not crowd out the meaningful superseded/current evidence we render.
    facts = [
        fact
        for fact in row.get("temporal_facts") or []
        if _text(fact.get("fact"))
    ]
    current = [fact for fact in facts if fact.get("is_current_belief") is True]
    chosen = (current + [fact for fact in facts if fact not in current])[:2]
    valid_at = next((fact.get("valid_at") for fact in chosen if fact.get("valid_at")), None)
    learned_at = next(
        (
            fact.get("created_at")
            for fact in chosen
            if fact.get("created_at")
        ),
        None,
    )
    normalized = [
        {**fact, "fact": _compact(fact.get("fact"), 240)}
        for fact in chosen
    ]
    return valid_at, learned_at, normalized


def _retrieval_entry(row: dict[str, Any], query_tokens: set[str]) -> dict[str, Any]:
    content = _query_compact(row.get("content") or row.get("name"), query_tokens, 650)
    valid_at, learned_at, temporal_facts = _temporal_fields(row)
    authority = "context"
    if row.get("is_superseded_view"):
        authority = "superseded"
    elif temporal_facts and not any(
        fact.get("is_current_belief") is True for fact in temporal_facts
    ):
        authority = "superseded"
    return {
        "id": _text(row.get("uuid")),
        "kind": _text(row.get("memory_type")) or "content",
        "authority": authority,
        "subject": _text(row.get("name")) or "retrieved memory",
        "value": content,
        "content": content,
        "valid_at": valid_at,
        "learned_at": learned_at,
        "retrieval_rank": row.get("rank"),
        "source_ids": [_text(row.get("uuid"))] if _text(row.get("uuid")) else [],
        "temporal_facts": temporal_facts,
    }


def _selected_typed_entry(entry: dict[str, Any]) -> dict[str, Any]:
    selected = dict(entry)
    if selected.get("source_quote"):
        selected["source_quote"] = _compact(selected["source_quote"], 500)
    if selected.get("content"):
        selected["content"] = _compact(selected["content"], 650)
    return selected


def _authority_ids(verdicts: Iterable[dict[str, Any]]) -> set[str]:
    """Extract durable identities without interpreting domain-specific verdict text."""
    return {
        identifier
        for verdict in verdicts
        for identifier in (
            _text(verdict.get("view_uuid")),
            _text(verdict.get("assertion_uuid")),
            _text(verdict.get("uuid")),
        )
        if identifier
    }


def _render_query_filtered_packet(
    query: str,
    sections: Iterable[dict[str, Any]],
) -> str:
    lines = [
        f"MEMORY PACKET {QUERY_FILTERED_PACKET_VERSION}",
        "MODE: query-filtered production recall evidence; read-only formatting",
        f"QUERY: {_compact(query, 500)}",
        "",
        "USAGE POLICY",
        *[f"- {rule}" for rule in _USAGE_POLICY],
    ]
    for section in sections:
        entries = section.get("entries") or []
        if not entries:
            continue
        lines.extend(("", f"[{_text(section.get('label')).upper()}]"))
        for entry in entries:
            lines.extend(_render_entry(entry))
    return "\n".join(lines)


def build_query_filtered_recall_packet(
    full_packet: dict[str, Any],
    retrieval_results: Iterable[dict[str, Any]],
    query: str,
    *,
    authority_layer: Iterable[dict[str, Any]] = (),
    event_authority_layer: Iterable[dict[str, Any]] = (),
    max_chars: int = DEFAULT_QUERY_PACKET_MAX_CHARS,
    max_general: int = 4,
) -> dict[str, Any]:
    """Join ranked production retrieval with typed graph authority under a hard budget.

    Retrieval remains the evidence selector.  The full packet contributes typed scalar,
    history, and event metadata only when the retrieved UUID or generic query relevance
    supports it.  This prevents the inspection packet's complete memory inventory from
    becoming an unranked answer-model context dump.
    """
    max_chars = max(2_000, int(max_chars))
    max_general = max(0, int(max_general))
    query = _text(query)
    query_tokens = _tokens(query)
    results = sorted(
        (dict(row) for row in retrieval_results),
        key=lambda row: int(row.get("rank") or 10_000),
    )
    retrieved_ids = {_text(row.get("uuid")) for row in results if _text(row.get("uuid"))}
    scalar_authority_ids = _authority_ids(authority_layer)
    event_authority_ids = _authority_ids(event_authority_layer)
    selected_ids = retrieved_ids | scalar_authority_ids | event_authority_ids
    source = _section_map(full_packet)

    current = [
        _selected_typed_entry(entry)
        for entry in source.get("current_state", [])
        if _text(entry.get("id")) in selected_ids
    ]
    # Archived/legacy recall contexts may not carry durable UUIDs. Keep a bounded lexical
    # fallback only for that degraded input shape; normal production packets are ID-driven.
    lexical_fallback = not selected_ids
    current_entry_ids = {_text(entry.get("id")) for entry in current}
    identity_current = [
        _selected_typed_entry(entry)
        for entry in source.get("current_state", [])
        if _text(entry.get("id")) not in current_entry_ids
        and _identity_relevant(entry, query_tokens)
    ]
    # Retrieval can return related content without returning the durable View UUID.
    # Add a small, identity-only authority fallback even when other durable IDs exist.
    current.extend(identity_current[:2])
    current_ids = {_text(entry.get("id")) for entry in current}
    selected_scalar_keys = {_scalar_key(entry) for entry in current}

    history_requested = bool(_HISTORY_INTENT.search(query))
    history = []
    if history_requested:
        history = [
            _selected_typed_entry(entry)
            for entry in source.get("change_history", [])
            if _scalar_key(entry) in selected_scalar_keys
            or _text(entry.get("id")).split(":", 1)[0] in selected_ids
        ]
        if not history and lexical_fallback:
            history = [
                _selected_typed_entry(entry)
                for entry in source.get("change_history", [])
                if _identity_relevant(entry, query_tokens)
            ]

    event_requested = bool(_EVENT_INTENT.search(query))
    events = []
    if event_requested:
        events = [
            _selected_typed_entry(entry)
            for entry in source.get("completed_events", [])
            if _text(entry.get("id")).split(":", 1)[0] in selected_ids
        ]
        if not events and lexical_fallback:
            events = [
                _selected_typed_entry(entry)
                for entry in source.get("completed_events", [])
                if _identity_relevant(entry, query_tokens)
            ]

    represented_ids = current_ids | {
        _text(entry.get("id")).split(":", 1)[0] for entry in history + events
    }
    general: list[dict[str, Any]] = []
    for row in results:
        uuid = _text(row.get("uuid"))
        if uuid and uuid in represented_ids:
            continue
        entry = _retrieval_entry(row, query_tokens)
        if len(general) < max_general:
            general.append(entry)

    candidates = {
        "current_state": current,
        "change_history": history,
        "completed_events": events,
        "general_content": general,
    }
    included: dict[str, list[dict[str, Any]]] = {key: [] for key in candidates}
    section_meta = {
        section_id: {"id": section_id, "label": label, "role": role}
        for section_id, label, role in _SECTION_DEFINITIONS
    }

    def rendered() -> tuple[list[dict[str, Any]], str]:
        sections = [
            {**section_meta[section_id], "entries": included[section_id]}
            for section_id, _, _ in _SECTION_DEFINITIONS
        ]
        return sections, _render_query_filtered_packet(query, sections)

    for section_id, _, _ in _SECTION_DEFINITIONS:
        for entry in candidates[section_id]:
            included[section_id].append(entry)
            _sections, text = rendered()
            if len(text) > max_chars:
                included[section_id].pop()

    sections, text = rendered()
    included_ids = [
        _text(entry.get("id"))
        for section in sections
        for entry in section.get("entries") or []
        if _text(entry.get("id"))
    ]
    candidate_count = sum(len(entries) for entries in candidates.values())
    return {
        "version": QUERY_FILTERED_PACKET_VERSION,
        "mode": "query_filtered",
        "production_recall_changed": False,
        "query": query,
        "source_packet_version": full_packet.get("version"),
        "budget": {"max_chars": max_chars, "actual_chars": len(text)},
        "selection": {
            "retrieval_count": len(results),
            "candidate_count": candidate_count,
            "included_count": len(included_ids),
            "omitted_count": max(0, candidate_count - len(included_ids)),
            "included_ids": included_ids,
            "history_intent": history_requested,
            "event_intent": event_requested,
            "selection_mode": "lexical_fallback" if lexical_fallback else "durable_ids",
        },
        "usage_policy": list(_USAGE_POLICY),
        "sections": sections,
        "text": text,
    }
