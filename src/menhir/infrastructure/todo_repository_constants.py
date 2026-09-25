"""Shared constants and query helpers for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor): the shared Cypher
fragments, relation whitelists, validation sets, and keyword-search helpers used
by the ``TodoRepository`` facade and its mixin modules. Every name here is
re-exported from ``todo_repository``, so existing import sites keep working
unchanged.
"""

from __future__ import annotations

import re

from menhir.domain.todo_location import TODO_LINK_RELATIONS

_VALID_PRIORITIES = frozenset({"low", "normal", "high"})
_VALID_STATUSES = frozenset({"open", "closed"})

#: Canonical todo-age expression. THE single source of truth: every read that
#: reports an age, flags staleness, or selects todos by age MUST use this and
#: never inline its own duration call.
#:
#: `duration.between(a, b)` returns a STRUCTURED duration whose months are
#: extracted first, so its `.days` component is only the sub-month remainder --
#: a todo created 2026-05-28 and read on 2026-09-02 is "3 months 5 days" and
#: reports `.days == 5`. That silently capped every age at ~31, made the
#: `> TODO_STALE_AFTER_DAYS` flag near-unreachable, and turned
#: `close_stale_todos(older_than_days=60)` into a permanent no-op.
#: `duration.inDays(a, b)` returns a day-only duration, so `.days` is the true
#: total. Verified against the live graph: see tests in tests/test_todo.py.
#:
#: Binds the node to the alias `n`; every query using it must name its :Todo `n`.
TODO_AGE_DAYS_CYPHER = "duration.inDays(datetime(n.created_at), datetime()).days"

#: Days open before a todo is flagged stale in read output. Passed as the
#: `$stale_after` query parameter rather than inlined, so the threshold has one
#: definition too.
TODO_STALE_AFTER_DAYS = 30

#: Inbound semantic relations a memory may declare against a todo, mapped to their
#: distinct edge types. Slice 1 ships reference relations only -- RESOLVES_TODO and
#: REOPENS_TODO belong with the lifecycle transaction in slice 2, because creating
#: them without one would imply a status change the edge alone must never make.
#:
#: Cypher cannot parameterize a relationship type, so this whitelist is also the
#: injection guard: a relation outside it never reaches a query string.
#:
#: Imported from domain.todo_location (CF-150 shape): the `link_memory_to_todo` MCP
#: tool validates its `relation` argument against the same mapping, and a second
#: hand-written copy there would agree until the day someone adds a relation to one.
_TODO_LINK_RELATIONS = TODO_LINK_RELATIONS

#: Lifecycle relations. Deliberately absent from _TODO_LINK_RELATIONS so they can
#: never be created by a bare link call -- they exist only as the evidence half of
#: resolve_todo / reopen_todo, which create the edge and move status together.
#: They are still readable, so a todo can show why it closed or reopened.
_TODO_LIFECYCLE_RELATIONS: dict[str, str] = {
    "resolves": "RESOLVES_TODO",
    "reopens": "REOPENS_TODO",
}

#: Every inbound relation type, for reads.
_ALL_TODO_INBOUND_EDGES: list[str] = [
    *_TODO_LINK_RELATIONS.values(),
    *_TODO_LIFECYCLE_RELATIONS.values(),
]

#: The one todo-to-todo edge. Every other edge on a :Todo points INWARD from a semantic
#: object, because knowledge belongs in memories and never accumulates inside the todo.
#: This one is deliberately different, and the exception is narrow enough to state exactly:
#: supersession is an IDENTITY fact ("this node replaced that node"), not a knowledge claim.
#: It is the same category as RESOLVES_TODO -- lifecycle, not meaning -- and it makes neither
#: todo describe the other. Admitting it was an owner decision on 2026-09-03, taken because
#: the inward-only alternative (both todos hung off the refiling memory) cannot express which
#: replacement belongs to which original when one memory refiles N todos into M.
#:
#: A todo still never becomes a semantic object: this edge is not recallable, and CF-247's
#: ADJACENCY_EDGE_TYPES allowlist (RELATES_TO, MENTIONS) excludes it from ranking entirely.
_TODO_SUPERSESSION_EDGE = "SUPERSEDED_BY"


# Words to skip when extracting keywords for entity matching
_STOPWORDS = frozenset({
    "about", "above", "after", "again", "against", "before", "being",
    "between", "could", "doing", "during", "having", "needs", "other",
    "their", "there", "these", "those", "under", "until", "using",
    "where", "which", "while", "would", "should", "shall", "might",
})


def _query_words(text: str) -> list[str]:
    """Extract meaningful words (>= 5 chars, not stopwords) for keyword search."""
    return [
        w for w in re.findall(r"\b[a-zA-Z]{5,}\b", text.lower())
        if w not in _STOPWORDS
    ]
