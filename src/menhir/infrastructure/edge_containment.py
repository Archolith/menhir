"""Drop edges whose fact is restated inside a fuller edge from the same extraction.

Some models restate a sub-fact of a fuller edge on a different endpoint pair ("jeans are from
Levi's" next to "user bought black jeans from Levi's"); Graphiti's exact-match and same-pair dedup
cannot catch that. An edge is dropped only when it sits on a different endpoint pair, every content
token, including numbers and names, appears in a surviving edge with compatible timestamps and
attributes, and neither fact carries a negation, modality or clause-embedding word.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX

_STOPWORDS = frozenset(
    {
        # articles, pronouns, possessive determiners
        "a", "an", "the", "i", "me", "my", "you", "your", "he", "him", "his", "she", "her",
        "it", "its", "we", "us", "our", "they", "them", "their", "this", "that", "these",
        "those", "user",
        # auxiliaries
        "is", "are", "was", "were", "be", "been", "being", "am", "has", "have", "had", "do",
        "does", "did", "will", "would", "can", "could", "should", "may", "might", "shall",
        # prepositions and conjunctions
        "and", "or", "but", "of", "to", "in", "on", "at", "by", "for", "with", "from", "as",
        "into", "about", "than", "then", "so", "also", "which", "who", "whom", "what", "while",
    }
)  # fmt: skip
_TOKEN = re.compile(r"[a-z0-9]+")
_POSSESSIVE = re.compile(r"'s\b")


def _normalize(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def fact_tokens(fact: str) -> frozenset[str]:
    """Content tokens of ``fact``; digits and negations are always content."""
    text = (fact or "").lower().replace("’", "'").replace("‘", "'")
    text = _POSSESSIVE.sub("", text)
    out = set()
    for raw in _TOKEN.findall(text):
        norm = _normalize(raw)
        if raw in _STOPWORDS or norm in _STOPWORDS:
            continue
        out.add(norm)
    return frozenset(out)


# Words that change whether or how a fact holds, or embed it in a clause the speaker does not
# assert. Word inclusion only suggests entailment for plain assertions: under negation it runs
# backwards ("does not like jazz at clubs" does not imply "does not like jazz"), and "denies
# that", "might" or "stopped" do not assert the inner fact. A pair where either fact carries one
# is kept. The list cannot be exhaustive; same-pair facts never reach it (see _compatible).
# Counted before stopword removal, because the modal verbs are stopwords.
_MARKERS = frozenset(
    {
        "not", "no", "never", "nor", "neither", "none", "nothing", "nobody", "nowhere",
        "without", "cannot", "t",
        "can", "could", "would", "should", "will", "may", "might", "shall", "must", "maybe",
        "perhaps", "possibly", "probably", "likely", "unlikely", "if", "unless",
        "plan", "plans", "planned", "planning", "intend", "intends", "intended", "want",
        "wants", "wanted", "hope", "hopes", "hoped", "wish", "wishes", "wished", "consider",
        "considering", "considered",
        "stop", "stopped", "quit", "ceased", "former", "formerly", "ex", "anymore",
        "previously", "used", "longer",
        # clause embedding and non-veridical predicates
        "that", "whether",
        "deny", "denies", "denied", "denying", "refuse", "refuses", "refused", "reject",
        "rejects", "rejected", "dispute", "disputes", "disputed",
        "claim", "claims", "claimed", "say", "says", "said", "tell", "tells", "told",
        "believe", "believes", "believed", "think", "thinks", "thought", "doubt", "doubts",
        "doubted", "suspect", "suspects", "suspected", "assume", "assumes", "assumed",
        "suppose", "supposes", "supposed", "guess", "guesses", "guessed", "imagine",
        "imagines", "imagined", "pretend", "pretends", "pretended", "dream", "dreams",
        "dreamed", "dreamt", "wonder", "wonders", "wondered", "unsure", "uncertain",
        "allegedly", "supposedly", "reportedly", "apparently", "seem", "seems", "seemed",
        "rumor", "rumored", "false", "falsely", "untrue", "fake", "wrong", "wrongly",
        "mistaken", "mistakenly", "lie", "lies", "lied",
    }
)  # fmt: skip


def fact_markers(fact: str) -> frozenset[str]:
    """Polarity and modality words of ``fact`` ("n't" counts as "not")."""
    text = (fact or "").lower().replace("’", "'").replace("‘", "'")
    out = set()
    for raw in _TOKEN.findall(text):
        if raw in _MARKERS:
            out.add("not" if raw in ("t", "cannot") else raw)
            if raw == "cannot":
                out.add("can")
    return frozenset(out)


def _compatible(kept: Any, dropped: Any) -> bool:
    if fact_markers(getattr(kept, "fact", "")) or fact_markers(getattr(dropped, "fact", "")):
        return False
    # Only cross-pair restatements are pruned. Facts on one pair (either direction, which word
    # bags cannot tell apart) are left to Graphiti's edge resolution, which compares meaning.
    kept_pair = (kept.source_node_uuid, kept.target_node_uuid)
    dropped_pair = (dropped.source_node_uuid, dropped.target_node_uuid)
    if set(dropped_pair) == set(kept_pair):
        return False
    for attr in ("valid_at", "invalid_at"):
        value = getattr(dropped, attr, None)
        if value is not None and value != getattr(kept, attr, None):
            return False
    kept_attrs = getattr(kept, "attributes", None) or {}
    dropped_attrs = getattr(dropped, "attributes", None) or {}
    return all(key in kept_attrs and kept_attrs[key] == v for key, v in dropped_attrs.items())


def _endpoints(edges: list[Any]) -> set[str]:
    out: set[str] = set()
    for edge in edges:
        out.add(edge.source_node_uuid)
        out.add(edge.target_node_uuid)
    return out


def prune_contained_edges(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    protected_uuids: Collection[str] = (),
) -> tuple[int, int]:
    """Mutates nodes, edges and index_map in place; returns (edges_pruned, nodes_dropped)."""
    if len(edges) < 2:
        return 0, 0
    facts = [getattr(edge, "fact", "") or "" for edge in edges]
    toks = [fact_tokens(fact) for fact in facts]
    # Richest first, so every kept edge is at least as rich as anything it absorbs.
    order = sorted(range(len(edges)), key=lambda i: (-len(toks[i]), -len(facts[i]), i))
    kept: list[int] = []
    keeper_of: dict[int, int] = {}
    for j in order:
        if facts[j].startswith(SYNTHETIC_FACT_PREFIX):
            continue  # structural membership edges are neither pruned nor keepers
        keeper = next(
            (
                i
                for i in kept
                if toks[j]
                and toks[j] <= toks[i]
                and _compatible(edges[i], edges[j])
            ),
            None,
        )
        if keeper is None:
            kept.append(j)
        else:
            keeper_of[j] = keeper
    if not keeper_of:
        return 0, 0

    for j, i in keeper_of.items():
        episodes = getattr(edges[i], "episodes", None)
        if isinstance(episodes, list):
            for uuid in getattr(edges[j], "episodes", None) or []:
                if uuid not in episodes:
                    episodes.append(uuid)

    before = _endpoints(edges)
    edges[:] = [edge for k, edge in enumerate(edges) if k not in keeper_of]
    orphaned = (before - _endpoints(edges)) - set(protected_uuids)
    node_count = len(nodes)
    nodes[:] = [node for node in nodes if node.uuid not in orphaned]
    for uuid in orphaned:
        index_map.pop(uuid, None)
    return len(keeper_of), node_count - len(nodes)
