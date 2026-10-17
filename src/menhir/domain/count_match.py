"""Related-count matching (counting plan unit 4R): pure rules shared by the fold and the runner.

A counted occurrence carries a decision (`count_status`) stored on its own :TypedAssertion node:
counted, repeat or same_event (of `count_target_id`), or uncertain. No decision yet means uncertain.
Only `counted` rows add to a View's total; uncertain rows are reported separately ("M more that may
be repeats"), so no happening is ever counted twice and none silently disappears.

Under the match flag a counted slot is keyed by the object family ("model kit"), not by the
extracted attribute, so "finished a kit" and "bought a kit" land in one View with per-action counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

COUNTED = "counted"
REPEAT = "repeat"
SAME_EVENT = "same_event"
UNCERTAIN = "uncertain"

#: every value `count_status` may hold.
COUNT_STATUSES: frozenset[str] = frozenset({COUNTED, REPEAT, SAME_EVENT, UNCERTAIN})

#: statuses that point at another happening (`count_target_id`).
TARGETED_STATUSES: frozenset[str] = frozenset({REPEAT, SAME_EVENT})

#: perceiver-version suffix while the match flag is on; flipping it re-reads history.
MATCH_VERSION_SUFFIX = "+m"

#: related counts sent per candidate, and per page in total (R3-4). Past either bound the candidate's
#: related set is truncated, and a truncated candidate may never be decided `counted`.
RELATED_PER_CANDIDATE = 10
RELATED_PER_PAGE = 40

#: counted rows pulled per page as the related pool; a full pool marks every candidate truncated.
RELATED_POOL_LIMIT = 2000

#: undecided occurrences decided per namespace pass (crash leftovers, repaired bindings).
UNDECIDED_SWEEP_LIMIT = 200

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: words that describe the action, not the object; dropped before family matching.
_ACTION_WORDS: frozenset[str] = frozenset({
    "bought", "buy", "buying", "purchased", "purchase", "finished", "finish", "finishing",
    "started", "start", "starting", "visited", "visit", "visiting", "completed", "complete",
    "made", "make", "built", "build", "did", "do", "went", "go", "got", "get", "attended",
    "attend", "watched", "watch", "read", "played", "play", "took", "take", "worked", "work",
})

_STOP_WORDS: frozenset[str] = frozenset({"a", "an", "the", "of", "my", "some", "another", "new"})


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def family_tokens(text: str) -> tuple[str, ...]:
    """Normalized object tokens: lowercase alphanumeric words, singular, action and stop words dropped.
    Order is kept so the family attribute is stable."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "").lower()):
        if raw in _ACTION_WORDS or raw in _STOP_WORDS:
            continue
        token = _singular(raw)
        if token and token not in out:
            out.append(token)
    return tuple(out)


def normalize_family(text: str) -> str:
    """The canonical family name stored on a decision: the family tokens joined by single spaces."""
    return " ".join(family_tokens(text))


def family_attribute(family: str, value_kind: str) -> str:
    """The counted slot's attribute for a family: `model_kit_count`, or `concert_spend` for money.
    Empty when the family has no tokens (the row then keeps its extracted attribute)."""
    tokens = family_tokens(family)
    if not tokens:
        return ""
    return "_".join(tokens) + ("_spend" if str(value_kind).strip().lower() == "money" else "_count")


def related_score(a_tokens: Iterable[str], b_tokens: Iterable[str]) -> int:
    """Signature hits between two families: the number of shared family tokens."""
    return len(set(a_tokens) & set(b_tokens))


def row_family(row: dict[str, Any]) -> str:
    """The family a stored occurrence row counts under: its decision's family, else its object."""
    return str(row.get("count_family") or "").strip() or str(row.get("count_object") or "").strip()


def match_slot_row(row: dict[str, Any]) -> dict[str, Any]:
    """The row as the match-mode fold sees it: a family-keyed attribute and a blank scope (scope and
    action are details, not key parts). Rows with no family keep their own attribute and scope.
    Returns a copy; the stored row is never mutated."""
    attribute = family_attribute(row_family(row), str(row.get("value_kind") or ""))
    if not attribute:
        return row
    shaped = dict(row)
    shaped["count_source_attribute"] = row.get("attribute")
    shaped["attribute"] = attribute
    shaped["scope"] = ""
    return shaped


def status_of(row: dict[str, Any]) -> str:
    """A row's decision; anything unknown or missing is uncertain."""
    status = str(row.get("count_status") or "").strip().lower()
    return status if status in COUNT_STATUSES else UNCERTAIN


def relation_tokens(row: dict[str, Any]) -> tuple[str, ...]:
    """Tokens used to relate two occurrences: the family's, else the extracted attribute's (minus
    its `_count`/`_spend` suffix), so a row with no object still relates to its slot-mates."""
    tokens = family_tokens(row_family(row))
    if tokens:
        return tokens
    attribute = str(row.get("attribute") or "")
    for suffix in ("_count", "_spend"):
        if attribute.endswith(suffix):
            attribute = attribute[: -len(suffix)]
    return family_tokens(attribute.replace("_", " "))


def _order_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("valid_at") or ""), str(row.get("episode_uuid") or ""),
            str(row.get("assertion_id") or ""))


@dataclass(frozen=True)
class RelatedSet:
    """One candidate and the related counts sent with it (best first)."""

    candidate: dict[str, Any]
    related: tuple[dict[str, Any], ...]
    #: a bound cut this candidate's related set (or the pool itself was cut); never `counted`.
    truncated: bool


def related_sets(
    candidates: list[dict[str, Any]], pool: list[dict[str, Any]], *,
    pool_truncated: bool = False,
    per_candidate: int = RELATED_PER_CANDIDATE, per_page: int = RELATED_PER_PAGE,
    cosine: dict[tuple[str, str], float] | None = None, min_cosine: float = 1.0,
) -> list[RelatedSet]:
    """Relate each candidate to the counted pool and to the page's EARLIER candidates (by
    valid_at, then turn, then id). Related = same subject, same value_kind, and at least one shared
    relation token or a `cosine[(candidate_id, other_id)]` of at least `min_cosine`. Ranked by
    shared tokens, then cosine, then assertion_id. A candidate past `per_candidate` related rows,
    or past the page's remaining `per_page` budget, is truncated."""
    ordered = sorted(candidates, key=_order_key)
    cosine = cosine or {}
    remaining = int(per_page)
    out: list[RelatedSet] = []
    for i, cand in enumerate(ordered):
        cand_id = str(cand.get("assertion_id") or "")
        cand_tokens = relation_tokens(cand)
        subject = str(cand.get("subject_uuid") or "")
        kind = str(cand.get("value_kind") or "").strip().lower()
        scored: list[tuple[int, float, str, dict[str, Any]]] = []
        seen: set[str] = {cand_id}
        for other in list(pool) + ordered[:i]:
            other_id = str(other.get("assertion_id") or "")
            if other_id in seen:
                continue
            seen.add(other_id)
            if (str(other.get("subject_uuid") or "") != subject
                    or str(other.get("value_kind") or "").strip().lower() != kind):
                continue
            score = related_score(cand_tokens, relation_tokens(other))
            cos = float(cosine.get((cand_id, other_id), 0.0))
            if score > 0 or cos >= min_cosine:
                scored.append((score, cos, other_id, other))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        truncated = bool(pool_truncated) or len(scored) > per_candidate
        sent = [row for _, _, _, row in scored[:per_candidate]]
        if len(sent) > remaining:
            sent = sent[:max(remaining, 0)]
            truncated = True
        remaining -= len(sent)
        out.append(RelatedSet(candidate=cand, related=tuple(sent), truncated=truncated))
    return out


def conservative_decisions(sets: list[RelatedSet]) -> list[dict[str, Any]]:
    """PR 1 code-only decisions (no LLM): a candidate with no related counts, no truncation and a
    family is `counted`; anything else is `uncertain`. Nothing here can count a happening twice."""
    decisions: list[dict[str, Any]] = []
    for rs in sets:
        cand = rs.candidate
        if not relation_tokens(cand):
            status, reason = UNCERTAIN, "no_family"
        elif rs.truncated:
            status, reason = UNCERTAIN, "truncated"
        elif rs.related:
            status, reason = UNCERTAIN, "related_undecided"
        else:
            status, reason = COUNTED, "no_related"
        decisions.append({
            "assertion_id": str(cand.get("assertion_id") or ""),
            "status": status,
            "target_id": "",
            "family": normalize_family(str(cand.get("count_object") or "")),
            "vote": "",
            "reason": reason,
            "truncated": rs.truncated,
        })
    return decisions


__all__ = [
    "COUNTED",
    "COUNT_STATUSES",
    "MATCH_VERSION_SUFFIX",
    "RELATED_PER_CANDIDATE",
    "RELATED_PER_PAGE",
    "RELATED_POOL_LIMIT",
    "REPEAT",
    "RelatedSet",
    "SAME_EVENT",
    "TARGETED_STATUSES",
    "UNCERTAIN",
    "UNDECIDED_SWEEP_LIMIT",
    "conservative_decisions",
    "family_attribute",
    "family_tokens",
    "match_slot_row",
    "normalize_family",
    "related_score",
    "related_sets",
    "relation_tokens",
    "row_family",
    "status_of",
]
