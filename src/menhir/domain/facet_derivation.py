"""Facet derivation — map graph facts + content into a MemoryFacetSet (R2, Phase 2a).

Deterministic and pure (no I/O). The caller supplies, per memory:
- **structural anchors** (from ``ANCHORED_TO`` -> file ``structure_path`` and file
  ``DEFINES`` -> symbol) — the exact code facts text cannot recover;
- **scope / belief / time metadata** (from node fields);
- the memory **content** (for interpretive facets: operation / object / actor /
  evidence, plus symbols mentioned in prose).

Both fashions land here: structural facets for anchored memories, interpretive/scope
facets for regular ones (validated corpus-wide — see the integration plan). The bulk
graph query that *supplies* the anchors is Phase 2b (service layer, graph I/O); this
module is the pure mapping and is unit-testable without a graph.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .facets import MemoryFacetSet

# PascalCase types, snake_case + SCREAMING_SNAKE identifiers, and `foo(` calls.
_PASCAL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_SCREAM_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]+)\s*\(")
_TEST_RE = re.compile(r"\b(?:test_\w+|\w+_test)\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_CALL_KEYWORDS = frozenset({"if", "for", "while", "return", "print", "def"})

_OPERATION_LEXICON: dict[str, tuple[str, ...]] = {
    "add": ("add", "adds", "added", "adding", "introduce", "introduced", "create", "created"),
    "fix": ("fix", "fixes", "fixed", "fixing", "patch", "patched"),
    "remove": ("remove", "removed", "removes", "removing", "delete", "deleted", "drop", "dropped"),
    "rename": ("rename", "renamed", "renames", "renaming"),
    "refactor": ("refactor", "refactored", "refactors", "refactoring"),
    "revert": ("revert", "reverted", "reverts", "reverting", "rollback", "rolled back"),
    "deprecate": ("deprecate", "deprecated", "deprecates"),
    "update": ("update", "updated", "updates", "updating", "bump", "bumped"),
    "configure": ("configure", "configured", "config", "set", "enable", "enabled", "disable", "disabled"),
}
_EVIDENCE_LEXICON: dict[str, str] = {
    "commit": "commit", "pr": "pr", "pull request": "pr", "test": "test", "log": "log",
    "logs": "log", "traceback": "log", "stack trace": "log", "issue": "issue",
    "benchmark": "benchmark", "bench": "benchmark", "trace": "trace",
}
_HISTORICAL_MARKERS = ("used to", "previously", "no longer", "deprecated", "formerly",
                       "back then", "old approach", "we removed")
_CURRENT_MARKERS = ("now ", "currently", "current ", "as of today", "today we")

_MAX_TEXT_SYMBOLS = 8
_MAX_OBJECTS = 6


def derive_facets(
    *,
    content: str = "",
    namespace: str | None = None,
    project: str | None = None,
    repo: str | None = None,
    belief_bucket: str | None = None,
    valid_time: str | None = None,
    learned_time: str | None = None,
    source_id: str | None = None,
    anchored_files: Iterable[str] = (),
    anchored_symbols: Iterable[str] = (),
) -> MemoryFacetSet:
    """Assemble a memory's facets from graph anchors + metadata + content.

    Precedence: graph/metadata facts win; text is only used where the graph is silent
    (interpretive facets, supplemental symbols, a fallback belief bucket / valid_time).
    """
    lower = content.lower()
    facets = MemoryFacetSet()

    # --- structural: graph anchors are authoritative; text symbols supplement ----------
    facets.file = {f for f in anchored_files if f}
    text_symbols = sorted(_extract_symbols(content))[:_MAX_TEXT_SYMBOLS]
    all_symbols = {s for s in anchored_symbols if s} | set(text_symbols)
    # Test names are their own facet regardless of ORIGIN: a `test_*`/`*_test` name that
    # arrives as a graph DEFINES symbol must land in ``test``, not ``symbol`` — otherwise a
    # query (whose test name comes from prose -> ``test``) never converges with the
    # candidate (whose test name came from the graph -> ``symbol``). Partition all symbol
    # sources through _TEST_RE so both sides classify identically.
    facets.test = {m.group(0) for m in _TEST_RE.finditer(content)} | {
        s for s in all_symbols if _TEST_RE.search(s)
    }
    facets.symbol = all_symbols - facets.test

    # --- scope / provenance / time: metadata wins, text is a fallback ------------------
    facets.namespace, facets.project, facets.repo = namespace, project, repo
    facets.source_id, facets.learned_time = source_id, learned_time
    iso = _ISO_RE.search(content)
    facets.valid_time = valid_time or (iso.group(0) if iso else None)
    facets.belief_bucket = belief_bucket or _extract_bucket(lower)

    # --- interpretive: recovered from prose (the genuinely lossy part) ------------------
    facets.operation = {
        lemma for lemma, forms in _OPERATION_LEXICON.items()
        if any(_word_present(form, lower) for form in forms)
    }
    facets.evidence_type = {
        etype for kw, etype in _EVIDENCE_LEXICON.items() if _word_present(kw, lower)
    }
    facets.object = _extract_objects(content)
    facets.actor = {"self"} if re.search(r"\b(?:i|we|our|my)\b", content, re.IGNORECASE) else set()
    return facets


def _extract_symbols(text: str) -> set[str]:
    symbols: set[str] = set(_PASCAL_RE.findall(text))
    symbols.update(n for n in _CALL_RE.findall(text) if n not in _CALL_KEYWORDS)
    symbols.update(_SNAKE_RE.findall(text))
    symbols.update(_SCREAM_RE.findall(text))
    return symbols


def _extract_objects(text: str) -> set[str]:
    objects = {m.group(1).strip() for m in _BACKTICK_RE.finditer(text)}
    return set(sorted(objects)[:_MAX_OBJECTS])


def _extract_bucket(lower: str) -> str | None:
    if any(marker in lower for marker in _HISTORICAL_MARKERS):
        return "historical"
    if any(marker in lower for marker in _CURRENT_MARKERS):
        return "current"
    return None


def _word_present(needle: str, haystack_lower: str) -> bool:
    return re.search(rf"\b{re.escape(needle.lower())}\b", haystack_lower) is not None
