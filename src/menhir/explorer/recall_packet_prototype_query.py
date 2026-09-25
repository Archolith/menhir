"""Query-filtered recall packet selection for the inspection-only Recall Lab packet.

Budgeted retrieval join and lexical selection, moved verbatim from
:mod:`menhir.explorer.recall_packet_prototype`, which re-exports the public
entry points for existing import sites.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from menhir.explorer.recall_packet_prototype_build import (
    _SECTION_DEFINITIONS,
    _USAGE_POLICY,
    _render_entry,
    _text,
)

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
