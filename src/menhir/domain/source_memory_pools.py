"""Anchor extraction and greedy pool grouping for the source-memory lane (pure).

Default-off (``MENHIR_FRONTIER_SOURCE_MEMORY_POOLS``). Operates ONLY on the memories
already returned by the source-memory search — never on the graph — and is fully
deterministic: same input order in, same pool ids out, regardless of hashing randomization.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

#: Step labels carry no cross-memory identity ("step 1" recurs in every session).
_STEP_LABEL_RE = re.compile(r"\b(step|turn|attempt)\s*\d+\b", re.IGNORECASE)

_FILE_PATH_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|csv|sql|json|md|txt|html|yaml|yml|cfg|toml)\b"
)
_URL_RE = re.compile(r"\b[a-z][\w+.-]*://\S+", re.IGNORECASE)
_QUOTED_RE = re.compile(r"`([^`\n]{2,60})`|\"([^\"\n]{2,60})\"")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*[._]\w+\b")
_TEST_ID_RE = re.compile(r"\btest_\w+")
_KEY_NUMBER_RE = re.compile(r"\b([A-Za-z_]\w*)=(\d+(?:\.\d+)?)\b")
_NOUN_NUMBER_RE = re.compile(r"\b([a-z]{2,})\s(\d{1,4})\b")

#: Minimum pool size; an anchor shared by one memory forms no pool.
MIN_POOL_SIZE = 2

_POOL_ID_PREFIX = "pool:"


def extract_anchors(text: str) -> set[str]:
    """Deterministic anchor set for one memory's text (lowercased).

    Anchors are the identity-bearing tokens two related memories share: file paths,
    URLs, quoted strings, dotted/underscored identifiers, ``test_*`` names,
    ``key=number`` pairs and lowercase noun+number phrases. Step labels are removed
    first so "step 2" never groups unrelated episodes.
    """
    if not text:
        return set()
    cleaned = _STEP_LABEL_RE.sub(" ", text)
    anchors: set[str] = set()

    for match in _FILE_PATH_RE.findall(cleaned):
        anchors.add(match.lower())
    for match in _URL_RE.findall(cleaned):
        anchors.add(match.rstrip(".,;:)"))
    for match in _QUOTED_RE.finditer(cleaned):
        quoted = match.group(1) or match.group(2)
        if quoted:
            anchors.add(quoted.strip().lower())
    for match in _IDENTIFIER_RE.findall(cleaned):
        anchors.add(match.lower())
    for match in _TEST_ID_RE.findall(cleaned):
        anchors.add(match.lower())
    for match in _KEY_NUMBER_RE.finditer(cleaned):
        anchors.add(f"{match.group(1)}={match.group(2)}".lower())
    for match in _NOUN_NUMBER_RE.finditer(cleaned):
        anchors.add(f"{match.group(1)} {match.group(2)}".lower())
    return anchors


def pool_id_for(member_uuids: list[str]) -> str:
    """Deterministic pool id over the sorted member uuids."""
    digest = hashlib.sha256(("v1|" + "|".join(sorted(member_uuids))).encode("utf-8"))
    return _POOL_ID_PREFIX + digest.hexdigest()[:16]


def assign_pools(
    memories: list[Any],
) -> list[tuple[Any, str | None, str | None]]:
    """Greedy anchor grouping over the RETURNED memories only.

    Repeatedly takes the anchor shared by the most remaining memories (ties:
    lexicographic), forms a pool from those memories when it is shared by >= 2, and
    removes them; stops when no anchor is shared by 2. Each memory appears in at most
    one pool. Returns ``(memory, pool_id, anchor)`` in input order; ungrouped memories
    carry ``(None, None)``.
    """
    anchors_by_index: list[set[str]] = [
        extract_anchors(str(getattr(m, "content", "") or "")) for m in memories
    ]
    remaining = list(range(len(memories)))
    results: list[tuple[Any, str | None, str | None]] = [
        (memory, None, None) for memory in memories
    ]

    while remaining:
        counts: dict[str, int] = {}
        for index in remaining:
            for anchor in anchors_by_index[index]:
                counts[anchor] = counts.get(anchor, 0) + 1
        best_count = max(counts.values(), default=0)
        if best_count < MIN_POOL_SIZE:
            break
        # Highest count first; ties broken lexicographically on the anchor.
        anchor = min(a for a, c in counts.items() if c == best_count)
        members = [i for i in remaining if anchor in anchors_by_index[i]]
        pool_id = pool_id_for([str(getattr(memories[i], "uuid", i)) for i in members])
        for i in members:
            results[i] = (memories[i], pool_id, anchor)
        remaining = [i for i in remaining if i not in set(members)]

    return results
