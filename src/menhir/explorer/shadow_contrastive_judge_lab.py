"""Shadow-composition CONTRASTIVE judge validation experiment (offline, NOT wired into
production).

Fourth step in the response chain:
  1. facet-instability report: exact-string-match label join never fires on real data.
  2. semantic-similarity lab: similarity separates unrelated content cleanly, but no single
     threshold achieves both perfect precision and recall -- the "boundaries" wrong_state_family
     decoy (lme-2698e78f) scores 0.5794, HIGHER than every true positive (max 0.4537).
  3. pairwise LLM-judge lab: a single (message, ONE candidate) binary MATCH/NO_MATCH call, tried
     3 different framings, never both preserved recall AND rejected the boundaries decoy. Best
     case (attempt 3, spot-check only): recall fixed, boundaries decoy still wrongly accepted.
  4. THIS module: per direct instruction, the pairwise design lets the boundaries decoy look
     plausible "in isolation" -- it never has to compete against the real answer. This tests a
     CONTRASTIVE design instead: show the model the message and ALL real candidates for that
     entity TOGETHER in one call, force it to extract a structured (subject, slot, value,
     evidence) for the message and for every candidate, and select which candidate(s) represent
     the PREVIOUS value of the exact slot being updated -- a comparative decision, not an
     isolated yes/no per candidate.

Design constraint (explicit, load-bearing): the extracted `slot` strings are NOT deterministically
string-compared after the call -- that would silently recreate the original Stage 1 exact-match
failure (free-text labels that never converge in surface form). The model's own
`selected_candidate_ids` is the only thing scored; `same_slot`/`slot`/`old_value`/`evidence` are
kept ONLY for audit and inspection, never fed back into a comparison function. See
score_contrastive_result() below, which reads nothing but selected_candidate_ids.

Test cases: one per fixture family (830/852/2698), each showing ALL of that family's real,
already-reviewed candidates together (one true positive + one of each DecoyType: wrong_state_
family, wrong_scope, stale, wrong_subject, missing_metadata) PLUS one cross-family "unrelated
control" candidate borrowed from a different family -- 7 candidates per test case, covering
every category the direct instruction asked for in a single contrastive call per family. This is
the real, already-validated ground truth from extraction_lab_eligibility_fixtures.py, not freshly
invented data.

This module contains only pure logic (fixture assembly, prompt construction, response parsing,
scoring). The runner that makes real LLM calls is scripts/run_shadow_contrastive_judge_lab.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from menhir.explorer.extraction_lab_eligibility_fixtures import (
    _830_scenarios,
    _852_scenarios,
    _2698_scenarios,
)
from menhir.explorer.extraction_lab_eligibility_selection import EligibilityScenario, StructuredFact

CONTRASTIVE_JUDGE_SYSTEM_PROMPT = """You are identifying which CANDIDATE FACT (if any) represents
the PREVIOUS value of the specific state that a CURRENT MESSAGE is updating -- like finding the
prior value of a field before an edit.

You will see ONE current message and SEVERAL candidate facts, all at once. Compare them against
EACH OTHER, not just against the message in isolation -- a candidate can look plausible on its
own but be about a different specific topic than a competing candidate that is the true prior
value.

Step 1: from the CURRENT MESSAGE, extract:
  - subject: who/what this is about
  - slot: the specific state/field being discussed or updated (be precise -- "current residence"
    is a different slot from "housing preferences"; "session frequency" is a different slot from
    "discussion topics" or "boundaries discussed")
  - new_value: the new value the message gives for that slot, if any
  - evidence: the exact span of the message that supports this

Step 2: for EVERY candidate fact given, extract the same shape (subject, slot, old_value,
evidence) plus same_slot: true only if this candidate is about the EXACT SAME slot as the
message's slot (same subject AND same specific state/field) -- not just the same subject, and
not just a related or nearby topic.

Step 3: select the candidate_id(s) (usually zero or one) that represent the previous value of
the exact slot the message is updating. A candidate must have same_slot=true to be selected. If
no candidate has the same slot, select nothing (empty list).

Return JSON only, matching this exact shape:
{"message": {"subject": "...", "slot": "...", "new_value": "...", "evidence": "..."},
 "candidates": [{"id": "...", "subject": "...", "slot": "...", "old_value": "...",
                 "same_slot": true, "evidence": "..."}],
 "selected_candidate_ids": ["..."]}"""


def contrastive_user_prompt(message: str, candidates: list[tuple[str, str]]) -> str:
    """candidates: list of (id, text) pairs, shown together."""
    lines = "\n".join(f'- id={cid!r}: {text}' for cid, text in candidates)
    return (
        f"CURRENT MESSAGE:\n{message}\n\n"
        f"CANDIDATE FACTS:\n{lines}\n\n"
        "Return JSON only, matching the schema in the system prompt."
    )


@dataclass(frozen=True)
class ContrastiveCandidate:
    candidate_id: str
    text: str
    category: str  # "positive" | DecoyType value | "cross_family_control"
    is_positive: bool


@dataclass(frozen=True)
class ContrastiveTestCase:
    """One family's message shown against ALL of that family's real candidates plus one
    cross-family control -- the full battery in a single contrastive call."""

    fixture_qid: str
    message: str
    candidates: tuple[ContrastiveCandidate, ...]

    def true_positive_ids(self) -> set[str]:
        return {c.candidate_id for c in self.candidates if c.is_positive}


def _distinct_candidates(scenarios: list[EligibilityScenario]) -> dict[str, StructuredFact]:
    """Every real candidate across a family's 7 scenario variants, deduped by fact TEXT (the
    same underlying candidate, e.g. the true-positive c1, recurs verbatim across multiple
    scenario variants with the same id; dedup by text is the more defensive key since a couple
    of ids get their scope field re-tagged between variants while the text stays identical)."""
    seen: dict[str, StructuredFact] = {}
    for scenario in scenarios:
        for candidate in scenario.candidates:
            if candidate.text not in seen:
                seen[candidate.text] = candidate
    return seen


def build_contrastive_test_cases() -> list[ContrastiveTestCase]:
    families: dict[str, list[EligibilityScenario]] = {
        "lme-830ce83f": _830_scenarios(),
        "lme-852ce960": _852_scenarios(),
        "lme-2698e78f": _2698_scenarios(),
    }
    correct_text_by_family: dict[str, str] = {}
    distinct_by_family: dict[str, dict[str, StructuredFact]] = {}
    message_by_family: dict[str, str] = {}
    for qid, scenarios in families.items():
        distinct_by_family[qid] = _distinct_candidates(scenarios)
        message_by_family[qid] = scenarios[0].current_message
        for scenario in scenarios:
            if scenario.correct_candidate_id is not None:
                correct = next(
                    c for c in scenario.candidates if c.id == scenario.correct_candidate_id
                )
                correct_text_by_family[qid] = correct.text
                break

    # Round-robin cross-family control: each family borrows one OTHER family's true-positive
    # text as its own "unrelated control" candidate -- a real, concrete sentence about a
    # different subject/topic entirely, not synthetic filler.
    family_order = list(families.keys())
    control_source = {
        family_order[i]: family_order[(i + 1) % len(family_order)]
        for i in range(len(family_order))
    }

    test_cases: list[ContrastiveTestCase] = []
    for qid in family_order:
        candidates: list[ContrastiveCandidate] = []
        correct_text = correct_text_by_family[qid]
        for idx, (text, fact) in enumerate(sorted(distinct_by_family[qid].items()), start=1):
            is_positive = text == correct_text
            category = "positive" if is_positive else fact.decoy_type.value
            candidates.append(ContrastiveCandidate(
                candidate_id=f"{qid}_cand{idx}", text=text, category=category,
                is_positive=is_positive,
            ))
        control_qid = control_source[qid]
        control_text = correct_text_by_family[control_qid]
        candidates.append(ContrastiveCandidate(
            candidate_id=f"{qid}_control", text=control_text,
            category="cross_family_control", is_positive=False,
        ))
        test_cases.append(ContrastiveTestCase(
            fixture_qid=qid, message=message_by_family[qid], candidates=tuple(candidates),
        ))
    return test_cases


def _extract_json_object(raw: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("contrastive judge response did not contain a JSON object")
    return json.loads(cleaned[start:end + 1])


@dataclass(frozen=True)
class ContrastiveResult:
    test_case: ContrastiveTestCase
    raw_response: str | None
    selected_ids: frozenset[str] | None  # None = call failed or response unparseable
    parsed_audit: dict | None  # full parsed JSON (message/candidates extraction) for inspection


def parse_contrastive_response(
    test_case: ContrastiveTestCase, raw: str | None,
) -> ContrastiveResult:
    if raw is None:
        return ContrastiveResult(test_case, None, None, None)
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        return ContrastiveResult(test_case, raw, None, None)
    selected = parsed.get("selected_candidate_ids")
    if not isinstance(selected, list):
        return ContrastiveResult(test_case, raw, None, parsed)
    return ContrastiveResult(
        test_case, raw, frozenset(str(s) for s in selected), parsed,
    )


@dataclass(frozen=True)
class ContrastiveScore:
    fixture_qid: str
    true_positive_preserved: bool  # every true-positive id was selected
    any_negative_selected: bool  # any selected id is NOT a true positive
    boundaries_selected: bool  # specifically: was the wrong_state_family decoy selected
    selected_ids: frozenset[str]
    selected_categories: tuple[str, ...]


def score_contrastive_result(result: ContrastiveResult) -> ContrastiveScore | None:
    """Scores ONLY against selected_candidate_ids -- deliberately never reads
    parsed_audit's same_slot/slot fields, per the explicit design constraint that
    deterministically comparing extracted slot strings would recreate the original
    exact-match failure. Returns None if the call/parse failed (a real failure, not a
    score of 0 -- must stay distinguishable in aggregate reporting)."""
    if result.selected_ids is None:
        return None
    tc = result.test_case
    true_ids = tc.true_positive_ids()
    by_id = {c.candidate_id: c for c in tc.candidates}
    selected_categories = tuple(
        by_id[cid].category for cid in result.selected_ids if cid in by_id
    )
    negatives_selected = result.selected_ids - true_ids
    boundaries_ids = {c.candidate_id for c in tc.candidates if c.category == "wrong_state_family"}
    return ContrastiveScore(
        fixture_qid=tc.fixture_qid,
        true_positive_preserved=true_ids.issubset(result.selected_ids),
        any_negative_selected=len(negatives_selected) > 0,
        boundaries_selected=len(result.selected_ids & boundaries_ids) > 0,
        selected_ids=result.selected_ids,
        selected_categories=selected_categories,
    )
