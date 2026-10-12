"""Blind attachment check for anchored time (MENHIR_ANCHORED_TIME_ATTACH, plan section 3.1).

A second model call says, per fact, which verbatim phrase of the turn dates that fact's start (a
state) or occurrence (a point event). It is blind: ``build_messages`` takes only the turn, the
speech date and the facts, never the resolver's items, so it cannot anchor on the resolver.

``decide`` is the deterministic veto. A resolver write survives only when both judgments point
at the same words (V1) and the check's relation matches the resolver's kind (V2). A failed or
missing answer never writes (V3). Everything here is pure; the call is in
``anchored_time_attach_client``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
import hashlib
from typing import Any

from menhir.infrastructure.anchored_time import (
    ATTACH_AGREE,
    ATTACH_REJECTED,
    ATTACH_UNVERIFIED,
    _fact_index,
    _fold,
    _occurrences,
    parse_output,
)

__all__ = [
    "ATTACH_AGREE",
    "ATTACH_PROMPT_VERSION",
    "ATTACH_REJECTED",
    "ATTACH_UNVERIFIED",
    "RELATIONS",
    "REQUIRED_RELATION",
    "agrees",
    "build_messages",
    "decide",
    "decide_all",
    "parse_answers",
]

SYSTEM = ("You read a conversation turn and say, for each listed fact, which words of the turn tell WHEN that "
          "fact's own event happened or its own state began. You never compute dates and never guess.")

INSTRUCTIONS = """For each FACT, find the phrase of the TURN that dates THIS fact, if there is one.

relation:
- "occurrence": the fact is a single event (bought, moved, met, finished, visited) and the TURN says when it happened.
- "start": the fact is a state that holds over a period (owns, lives, works as, has been, feels, is learning) and the TURN says when that state BEGAN.
- "plan": the fact is intended or in the future, and the TURN says when.
- "none": the TURN says nothing about when THIS fact's event happened or when its state began.

expression: the shortest phrase copied character for character from the TURN that gives that time, or null when relation is "none". A time measured from another event counts ("two days after the move", "ever since").

Rules:
- A time belongs to the event of its own clause. Never give a fact the time of a different event, even in the same sentence. "I repainted the shed last month and the garden looks great": the garden looking great has relation "none".
- A time when a state was seen, said or mentioned is not when it began. "Yesterday I noticed the roof leaks": the noticing happened yesterday; the leaking has no start.
- For a state, a duration that runs up to now gives its start ("I've worked there for two years" -> "start", "for two years"). A time that only shows the state already held then is not its start.
- "since" or "as" meaning "because" ("since you asked, I ...") is not a time.
- A number that is part of a name, model or title ("a 1998 Volvo", "the 2021 edition") is not a time.
- Requests, questions and wishes ("wants ideas for next month") get "none" for the asking.
- Answer every fact, in order, exactly once.

Return JSON only: {"facts": [{"i": <fact number>, "relation": "...", "expression": "..." or null}]}

Example
SPEECH_TIME: 2022-05-11 (Wednesday)
TURN: I adopted a beagle in March, and since then I've been walking every morning. My sister thinks the 2019 model is better. I need advice on a trip next week.
FACTS:
0. The user adopted a beagle.
1. The user walks every morning.
2. The user's sister prefers the 2019 model.
3. The user wants trip advice.
OUTPUT: {"facts": [
 {"i": 0, "relation": "occurrence", "expression": "in March"},
 {"i": 1, "relation": "start", "expression": "since then"},
 {"i": 2, "relation": "none", "expression": null},
 {"i": 3, "relation": "none", "expression": null}]}
"""

#: Changes with any wording change; it is part of the cache key and the persisted contract.
ATTACH_PROMPT_VERSION = hashlib.sha256(f"{SYSTEM}\n{INSTRUCTIONS}".encode("utf-8")).hexdigest()[:8]

RELATIONS = frozenset({"start", "occurrence", "plan", "none"})

#: V2: the relation a write needs, by resolver kind. A plan never reaches the check.
REQUIRED_RELATION = {"state": "start", "point_event": "occurrence"}


def build_messages(turn: str, speech_date: str, facts: list[str]) -> list[dict]:
    """The blind prompt. Takes no resolver output by construction."""
    d = date.fromisoformat(speech_date[:10])
    listed = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    user = (f"{INSTRUCTIONS}\nNow the real input.\nSPEECH_TIME: {d.isoformat()} ({d.strftime('%A')})\n"
            f"TURN: {turn}\nFACTS:\n{listed}\nOUTPUT:")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse_answers(content: str) -> dict[int, dict]:
    """Answers keyed by fact index as ``{"relation", "expression"}``. Raises on unparseable output.

    An answer with an unknown relation, or a fact answered twice, is dropped: a dropped fact is a
    missing answer, which ``decide`` treats as unverified (V3).
    """
    out = parse_output(content)
    if not isinstance(out, dict):
        raise ValueError("attach output is not a JSON object")
    answers: dict[int, dict] = {}
    seen: set[int] = set()
    duplicated: set[int] = set()
    for it in out.get("facts") or []:
        if not isinstance(it, dict):
            continue
        i = _fact_index(it.get("i"))
        if i is None:
            continue
        if i in seen:
            duplicated.add(i)
        seen.add(i)
        relation = it.get("relation")
        relation = relation.strip().lower() if isinstance(relation, str) else None
        expression = it.get("expression")
        if relation not in RELATIONS:
            continue
        answers[i] = {"relation": relation,
                      "expression": expression if isinstance(expression, str) and expression.strip() else None}
    for i in duplicated:
        answers.pop(i, None)
    return answers


def agrees(turn: str, resolver_expression: str | None, check_expression: str | None) -> bool:
    """V1: both phrases are in the turn and one contains the other at the same position."""
    if not (isinstance(resolver_expression, str) and isinstance(check_expression, str)):
        return False
    low = _fold(turn or "")
    a, b = _fold(resolver_expression).strip(), _fold(check_expression).strip()
    if not a or not b:
        return False
    spans_b = [(p, p + len(b)) for p in _occurrences(low, b)]
    for pa in _occurrences(low, a):
        ea = pa + len(a)
        for pb, eb in spans_b:
            if (pa <= pb and eb <= ea) or (pb <= pa and ea <= eb):
                return True
    return False


def decide(turn: str, kind: str, resolver_expression: str | None, answer: Mapping[str, Any] | None) -> str:
    """One candidate's decision: ``ATTACH_AGREE`` writes; anything else keeps Graphiti's time."""
    if not answer:
        return ATTACH_UNVERIFIED  # V3
    if answer.get("relation") != REQUIRED_RELATION.get(kind):
        return ATTACH_REJECTED  # V2
    if not agrees(turn, resolver_expression, answer.get("expression")):
        return ATTACH_REJECTED  # V1
    return ATTACH_AGREE


def decide_all(turn: str, candidates: Iterable[Any], answers: Mapping[int, Mapping[str, Any]] | None) -> dict[int, str]:
    """Decisions by fact index for ``plan_overlay``'s ``attach`` argument.

    ``candidates`` are the results a plan without the check would write (``written``).
    ``answers`` is ``None`` when the check call failed: every candidate is unverified (V3).
    """
    out: dict[int, str] = {}
    for result in candidates:
        answer = None if answers is None else answers.get(result.fact_index)
        out[result.fact_index] = decide(turn, result.kind, result.expression, answer)
    return out
