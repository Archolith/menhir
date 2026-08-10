"""Phase 4b: structured eligibility filtering before ranking (menhir-extraction-context-
ablation-handoff.md, per direct redesign instruction, 2026-07-16).

Phase 4's first selector prototype showed a precise diagnosis: subject discrimination is
solved (100% correct rejecting wrong-subject decoys); the open problem is an OPEN-SET
selection problem, not a reranking problem -- when every candidate is a bad match, a
forced-choice LLM ranker tends to pick the least-bad one instead of abstaining. This
module tests the fix: hard-filter candidates on structured metadata (subject, facet,
state_family, scope, bitemporal validity) BEFORE any ranking, and only fall back to an
LLM for genuine residual ambiguity among candidates that already passed the filter.

Four selectors compared, per direct instruction:
  A. llm_only               -- Phase 4's original prototype (prose only, no metadata shown)
  B. structured_only         -- pure rule-based eligibility filter, no LLM call at all
  C. structured_then_llm     -- filter first; LLM only consulted if 2+ candidates survive
  D. oracle                  -- always returns ground truth; upper-bound sanity reference

Two metrics, reported separately, per direct instruction (precision prioritized over
coverage -- "a system that injects context less often but is almost always correct is
safer than one that eagerly injects a nearby fact"):
  injection precision = of trials where something was injected, how often it was correct
  injection coverage  = of trials where a correct candidate existed, how often it was found

Seven scenario types per fixture (expanded from Phase 4's five, per direct instruction):
  1. one exact compatible fact
  2. same subject + same facet, WRONG state_family
  3. same subject + same state_family, WRONG scope (different real-world event)
  4. stale vs. current, same subject/facet/state_family/scope -- with REAL valid_at/
     expired_at timestamps this time, not word cues ("initially" vs "previously")
  5. only near-neighbor decoys (close in meaning, still wrong) -- no correct candidate
  6. no candidate mentions the target subject at all -- no correct candidate
  7. a candidate with missing/incomplete metadata -- must not be trusted by default

Scope note (unchanged from Phase 4, reconfirmed): entirely inside Recall Labs, hand-
authored candidate pools, no production graph schema changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class DecoyType(str, Enum):
    NONE = "none"
    WRONG_SUBJECT = "wrong_subject"
    WRONG_FACET = "wrong_facet"
    WRONG_STATE_FAMILY = "wrong_state_family"
    WRONG_SCOPE = "wrong_scope"
    STALE = "stale"
    MISSING_METADATA = "missing_metadata"


@dataclass(frozen=True)
class StructuredFact:
    id: str
    subject: str
    facet: str | None
    state_family: str | None
    scope: str | None  # which real-world event/thing this claim is about; None = unscoped
    text: str
    valid_at: str | None = None   # ISO timestamp: when this became true
    expired_at: str | None = None  # ISO timestamp: when this stopped being true (None = still valid)
    decoy_type: DecoyType = DecoyType.NONE


@dataclass(frozen=True)
class EligibilityScenario:
    scenario_id: str
    fixture_qid: str
    current_message: str
    reference_time: str  # ISO timestamp of the current message
    target_subject: str
    target_facet: str
    target_state_family: str
    target_scope: str | None  # None = scope not required to distinguish this scenario
    candidates: tuple[StructuredFact, ...]
    correct_candidate_id: str | None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Structured eligibility filter (selectors B and C's hard-filter stage)
# ---------------------------------------------------------------------------

def is_eligible(scenario: EligibilityScenario, candidate: StructuredFact) -> bool:
    """Hard eligibility gate. Per direct instruction: 'When metadata is absent or
    contradictory, abstention should be the default' -- a candidate missing metadata
    needed to confirm a required dimension is INELIGIBLE, not passed through."""
    if candidate.subject != scenario.target_subject:
        return False
    if candidate.facet is None or candidate.facet != scenario.target_facet:
        return False
    if candidate.state_family is None or candidate.state_family != scenario.target_state_family:
        return False
    if scenario.target_scope is not None:
        # Scope discrimination is required for this scenario -- a candidate with no
        # scope tag can't confirm it matches, so it fails safe (ineligible).
        if candidate.scope is None or candidate.scope != scenario.target_scope:
            return False
    # Bitemporal validity: candidate must have been true at/before the reference time
    # and not yet expired by then. Missing valid_at is treated as "unknown when this
    # became true" -- NOT automatically eligible; per the same abstention-by-default
    # principle, an un-dated claim about a temporally-sensitive state_family cannot be
    # confirmed current.
    ref = _parse(scenario.reference_time)
    valid_at = _parse(candidate.valid_at)
    if valid_at is None:
        return False
    if ref is not None and valid_at > ref:
        return False
    expired_at = _parse(candidate.expired_at)
    if expired_at is not None and ref is not None and expired_at <= ref:
        return False
    return True


def eligible_candidates(scenario: EligibilityScenario) -> list[StructuredFact]:
    return [c for c in scenario.candidates if is_eligible(scenario, c)]


def _most_recent(candidates: list[StructuredFact]) -> StructuredFact:
    """Tie-break among multiple eligible candidates: prefer the one with the latest
    valid_at (all eligible candidates have a parseable valid_at by construction of
    is_eligible, so this is always well-defined)."""
    return max(candidates, key=lambda c: _parse(c.valid_at) or datetime.min.replace(tzinfo=None))


# ---------------------------------------------------------------------------
# Selector A: LLM-only (Phase 4's original prototype, prose-only, no metadata shown)
# ---------------------------------------------------------------------------

_LLM_ONLY_SYSTEM_PROMPT = """You are a context-selection component for a conversational
memory system. You are given a CURRENT MESSAGE and a list of CANDIDATE FACTS from prior
conversation. Your job is to select AT MOST ONE candidate fact that gives useful PRIOR
context for correctly interpreting the CURRENT MESSAGE -- including recognizing when the
CURRENT MESSAGE is UPDATING, CORRECTING, or CONTRADICTING that prior fact.

IMPORTANT: an apparent disagreement between a candidate and the CURRENT MESSAGE is NOT a
reason to reject the candidate -- it is usually exactly the situation this prior context
is needed for.

Rules:
- Select a candidate only if it is about the SAME subject/entity and SAME specific topic
  as what the CURRENT MESSAGE discusses -- not just a broadly similar topic.
- If no candidate is a clear, specific match, select NOTHING. Do not guess or pick the
  closest-sounding option just because something in the list is topically adjacent.

Return JSON only: {"selected_id": "<candidate id>" or null, "reasoning": "brief"}"""


def _llm_only_prompt(scenario: EligibilityScenario) -> str:
    lines = "\n".join(
        f'- id="{c.id}": subject="{c.subject}": {c.text}' for c in scenario.candidates
    )
    return (
        f"CURRENT MESSAGE:\n{scenario.current_message}\n\n"
        f"CANDIDATE FACTS:\n{lines}\n\n"
        'Return JSON only, e.g. {"selected_id": "c1", "reasoning": "..."} '
        'or {"selected_id": null, "reasoning": "..."}'
    )


def _parse_llm_response(raw: str) -> tuple[str | None, str]:
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("selector did not return a JSON object")
    parsed = json.loads(cleaned[start:end + 1])
    selected_id = parsed.get("selected_id")
    if selected_id is not None and not isinstance(selected_id, str):
        raise ValueError(f"selected_id must be string or null, got {type(selected_id)}")
    return (selected_id, str(parsed.get("reasoning") or ""))


async def _call_llm_select(llm_backend: object, scenario: EligibilityScenario, candidates: list[StructuredFact]) -> tuple[str | None, str]:
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured selector LLM backend has no create_chat_completion")
    restricted_scenario = EligibilityScenario(
        scenario.scenario_id, scenario.fixture_qid, scenario.current_message,
        scenario.reference_time, scenario.target_subject, scenario.target_facet,
        scenario.target_state_family, scenario.target_scope,
        tuple(candidates), scenario.correct_candidate_id,
    )
    raw = await create(
        system_prompt=_LLM_ONLY_SYSTEM_PROMPT,
        user_prompt=_llm_only_prompt(restricted_scenario),
        operation="extraction_lab_eligibility_selector",
        max_tokens=300,
        temperature=0.0,
    )
    return _parse_llm_response(raw)


def _finalize(scenario: EligibilityScenario, selected_id: str | None, reasoning: str) -> dict:
    valid_ids = {c.id for c in scenario.candidates}
    if selected_id is not None and selected_id not in valid_ids:
        selected_id = f"INVALID:{selected_id}"
    selected = next((c for c in scenario.candidates if c.id == selected_id), None)
    return {
        "scenario_id": scenario.scenario_id,
        "fixture_qid": scenario.fixture_qid,
        "selected_id": selected_id,
        "correct_candidate_id": scenario.correct_candidate_id,
        "correct": selected_id == scenario.correct_candidate_id,
        "is_decoy_selection": selected is not None and selected.decoy_type != DecoyType.NONE,
        "decoy_type": selected.decoy_type.value if selected else None,
        "reasoning": reasoning,
    }


async def select_llm_only(llm_backend: object, scenario: EligibilityScenario) -> dict:
    """Selector A: unchanged from Phase 4 -- sees only prose, no structured metadata."""
    selected_id, reasoning = await _call_llm_select(llm_backend, scenario, list(scenario.candidates))
    result = _finalize(scenario, selected_id, reasoning)
    result["selector"] = "llm_only"
    result["llm_calls"] = 1
    return result


def select_structured_only(scenario: EligibilityScenario) -> dict:
    """Selector B: pure rule-based eligibility filter + recency tie-break. No LLM."""
    eligible = eligible_candidates(scenario)
    if not eligible:
        result = _finalize(scenario, None, "no candidate passed structured eligibility filter")
    elif len(eligible) == 1:
        result = _finalize(scenario, eligible[0].id, "unique eligible candidate")
    else:
        chosen = _most_recent(eligible)
        result = _finalize(scenario, chosen.id, f"{len(eligible)} eligible candidates; chose most recent by valid_at")
    result["selector"] = "structured_only"
    result["llm_calls"] = 0
    return result


async def select_structured_then_llm(llm_backend: object, scenario: EligibilityScenario) -> dict:
    """Selector C: hard-filter first; only calls the LLM if genuine ambiguity remains
    (2+ eligible candidates that structured filtering + recency couldn't resolve to one).
    In practice this means the LLM is called rarely -- structure resolves most cases."""
    eligible = eligible_candidates(scenario)
    if not eligible:
        result = _finalize(scenario, None, "no candidate passed structured eligibility filter")
        result["selector"] = "structured_then_llm"
        result["llm_calls"] = 0
        return result
    if len(eligible) == 1:
        result = _finalize(scenario, eligible[0].id, "unique eligible candidate (no LLM needed)")
        result["selector"] = "structured_then_llm"
        result["llm_calls"] = 0
        return result
    # 2+ eligible candidates with potentially tied recency -- ask the LLM to pick among
    # ONLY the eligible survivors (never the ones the filter already excluded).
    selected_id, reasoning = await _call_llm_select(llm_backend, scenario, eligible)
    result = _finalize(scenario, selected_id, f"[{len(eligible)} eligible after filter] {reasoning}")
    result["selector"] = "structured_then_llm"
    result["llm_calls"] = 1
    return result


def select_oracle(scenario: EligibilityScenario) -> dict:
    """Selector D: always returns ground truth. Upper-bound sanity reference -- should
    score 100% precision and 100% coverage by construction; if it doesn't, the scoring
    code itself has a bug, not the selector."""
    result = _finalize(scenario, scenario.correct_candidate_id, "oracle: ground truth")
    result["selector"] = "oracle"
    result["llm_calls"] = 0
    return result
