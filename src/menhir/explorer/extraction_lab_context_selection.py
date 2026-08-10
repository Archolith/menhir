"""Phase 4 (context selection): the actual open problem after Phase 2/3.

Phase 2 confirmed a MECHANISM (a compact, natively-delivered prior fact reliably helps
extraction). It did not confirm a SYSTEM (that Menhir can automatically choose the
right fact). This module tests the selection step in isolation, per direct
instruction (2026-07-16): given a pool of CANDIDATE facts (some correct, some
deliberate decoys), can an LLM-based selector pick the single correct one, or
correctly decline to pick anything when nothing correct exists?

Scope note: per direct confirmation (2026-07-16 AskUserQuestion), this stays entirely
inside Recall Labs -- hand-authored candidate pools, no real graph facet/fact storage,
no production schema changes (the original handoff's explicit exclusion). This tests
whether SELECTION is tractable at all; real graph-backed retrieval is a separate,
later, explicitly-scoped decision.

Two gates, measured independently (this is the point -- finding 4 from Phase 2 showed
the extractor can succeed for the WRONG reason, so downstream extraction success alone
cannot validate the selector):
  - selection recall:    was the correct fact present in the candidate pool at all?
                          (a property of pool construction here, reported for
                          traceability, not really "measured" since we control it)
  - selection precision: given the pool, did the selector choose the correct fact (or
                          correctly choose nothing), rejecting the decoys?

Five required negative-control scenarios per fixture (menhir-extraction-context-
ablation-handoff.md's Phase 4 "Required negative-control test matrix"):
  1. one correct fact only
  2. same subject, multiple facets (must pick the matching facet)
  3. same facet, multiple subjects (the false-grounding trap, made explicit)
  4. stale + current fact, same subject/facet (must prefer current)
  5. no relevant fact at all (must select nothing, not a loosely-adjacent decoy)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class DecoyType(str, Enum):
    NONE = "none"  # not a decoy -- the correct answer
    WRONG_SUBJECT = "wrong_subject"
    WRONG_FACET = "wrong_facet"
    STALE = "stale"  # correct subject/facet, but superseded by a newer fact in the pool


@dataclass(frozen=True)
class CandidateFact:
    id: str
    subject: str
    facet: str
    state_family: str
    text: str
    decoy_type: DecoyType = DecoyType.NONE


@dataclass(frozen=True)
class SelectionScenario:
    scenario_id: str  # "1_one_correct", "2_same_subject_multi_facet", etc.
    fixture_qid: str
    current_message: str
    target_subject: str
    target_facet: str
    candidates: tuple[CandidateFact, ...]
    correct_candidate_id: str | None  # None means the correct selection is "nothing"


# ---------------------------------------------------------------------------
# Per-fixture candidate pools
# ---------------------------------------------------------------------------

_830CE83F_MSG = (
    "user: Miami Beach sounds fun, but I've been there before. I'm thinking of "
    "somewhere more relaxed. My friend Rachel actually just moved back to the "
    "suburbs again, so I was thinking of somewhere not too far from a major city. "
    "Any suggestions?"
)

_830CE83F_CANDIDATES_S1 = (
    CandidateFact("c1", "Rachel", "Housing", "Current residence",
                  "Rachel previously moved to an apartment in Chicago."),
)
_830CE83F_CANDIDATES_S2 = _830CE83F_CANDIDATES_S1 + (
    CandidateFact("c2", "Rachel", "Employment", "Current job",
                  "Rachel recently changed jobs to a marketing role.", DecoyType.WRONG_FACET),
    CandidateFact("c3", "Rachel", "Health", "Fitness routine",
                  "Rachel mentioned she's been going to yoga classes.", DecoyType.WRONG_FACET),
)
_830CE83F_CANDIDATES_S3 = _830CE83F_CANDIDATES_S1 + (
    CandidateFact("c4", "Daniel", "Housing", "Current residence",
                  "A coworker named Daniel moved to a new apartment across town.", DecoyType.WRONG_SUBJECT),
    CandidateFact("c5", "Priya", "Housing", "Current residence",
                  "Priya recently moved into a house with her partner.", DecoyType.WRONG_SUBJECT),
)
_830CE83F_CANDIDATES_S4 = (
    CandidateFact("c6", "Rachel", "Housing", "Current residence",
                  "Rachel mentioned she used to live in Denver before moving to Chicago.", DecoyType.STALE),
    CandidateFact("c1", "Rachel", "Housing", "Current residence",
                  "Rachel previously moved to an apartment in Chicago."),
)
_830CE83F_CANDIDATES_S5 = (
    CandidateFact("c2", "Rachel", "Employment", "Current job",
                  "Rachel recently changed jobs to a marketing role.", DecoyType.WRONG_FACET),
    CandidateFact("c4", "Daniel", "Housing", "Current residence",
                  "A coworker named Daniel moved to a new apartment across town.", DecoyType.WRONG_SUBJECT),
)

_852CE960_MSG = (
    "user: I'm planning to move into my new home soon and I need to set up cable "
    "and TV services. Can you recommend some providers in my area and their prices? "
    "By the way, I'm really looking forward to finally owning a home, it's been a "
    "long process, but it'll be worth it to have a backyard like the one I'll have "
    "- remember when I got pre-approved for $400,000 from Wells Fargo?"
)

_852CE960_CANDIDATES_S1 = (
    CandidateFact("c1", "user", "Mortgage", "Pre-approval amount",
                  "The user was previously pre-approved for a $350,000 mortgage from Wells Fargo."),
)
_852CE960_CANDIDATES_S2 = _852CE960_CANDIDATES_S1 + (
    CandidateFact("c2", "user", "Home inspection", "Inspection findings",
                  "The user's home inspection found minor issues with the roof and plumbing.", DecoyType.WRONG_FACET),
    CandidateFact("c3", "user", "Closing costs", "Negotiated credit",
                  "The user negotiated a $2,000 credit toward closing costs.", DecoyType.WRONG_FACET),
)
_852CE960_CANDIDATES_S3 = _852CE960_CANDIDATES_S1 + (
    CandidateFact("c4", "user's sister", "Mortgage", "Pre-approval amount",
                  "The user's sister got pre-approved for a mortgage from a different bank.", DecoyType.WRONG_SUBJECT),
)
_852CE960_CANDIDATES_S4 = (
    CandidateFact("c5", "user", "Mortgage", "Pre-approval amount",
                  "The user was initially pre-approved for a $325,000 mortgage from Wells Fargo.", DecoyType.STALE),
    CandidateFact("c1", "user", "Mortgage", "Pre-approval amount",
                  "The user was previously pre-approved for a $350,000 mortgage from Wells Fargo."),
)
_852CE960_CANDIDATES_S5 = (
    CandidateFact("c2", "user", "Home inspection", "Inspection findings",
                  "The user's home inspection found minor issues with the roof and plumbing.", DecoyType.WRONG_FACET),
    CandidateFact("c4", "user's sister", "Mortgage", "Pre-approval amount",
                  "The user's sister got pre-approved for a mortgage from a different bank.", DecoyType.WRONG_SUBJECT),
)

_2698E78F_MSG = (
    "user: I see what you're doing here, but I think it's a bit too structured for "
    "me. I'd rather have some flexibility in my schedule. Can you help me come up "
    "with a more general plan to prioritize my tasks and set boundaries, rather "
    "than a strict schedule? And by the way, speaking of boundaries, I see Dr. "
    "Smith every week, and she's been helping me work on this stuff."
)

_2698E78F_CANDIDATES_S1 = (
    CandidateFact("c1", "Dr. Smith", "Therapy schedule", "Session frequency",
                  "The user previously said their therapy sessions with Dr. Smith are every two weeks."),
)
_2698E78F_CANDIDATES_S2 = _2698E78F_CANDIDATES_S1 + (
    CandidateFact("c2", "Dr. Smith", "Therapy topic", "Discussion focus",
                  "The user has been discussing setting healthy boundaries with Dr. Smith.", DecoyType.WRONG_FACET),
    CandidateFact("c3", "user", "Self-care", "Sleep routine",
                  "The user has been trying journaling and a consistent sleep schedule.", DecoyType.WRONG_FACET),
)
_2698E78F_CANDIDATES_S3 = _2698E78F_CANDIDATES_S1 + (
    CandidateFact("c4", "user's friend", "Therapy schedule", "Session frequency",
                  "A friend of the user sees her own therapist every other week.", DecoyType.WRONG_SUBJECT),
)
_2698E78F_CANDIDATES_S4 = (
    CandidateFact("c5", "Dr. Smith", "Therapy schedule", "Session frequency",
                  "The user originally scheduled therapy with Dr. Smith on a monthly basis.", DecoyType.STALE),
    CandidateFact("c1", "Dr. Smith", "Therapy schedule", "Session frequency",
                  "The user previously said their therapy sessions with Dr. Smith are every two weeks."),
)
_2698E78F_CANDIDATES_S5 = (
    CandidateFact("c2", "Dr. Smith", "Therapy topic", "Discussion focus",
                  "The user has been discussing setting healthy boundaries with Dr. Smith.", DecoyType.WRONG_FACET),
    CandidateFact("c4", "user's friend", "Therapy schedule", "Session frequency",
                  "A friend of the user sees her own therapist every other week.", DecoyType.WRONG_SUBJECT),
)


def _build_scenarios(
    qid: str, message: str, subject: str, facet: str,
    s1, s2, s3, s4, s5,
) -> list[SelectionScenario]:
    return [
        SelectionScenario(f"{qid}_1_one_correct", qid, message, subject, facet, s1, "c1"),
        SelectionScenario(f"{qid}_2_same_subject_multi_facet", qid, message, subject, facet, s2, "c1"),
        SelectionScenario(f"{qid}_3_same_facet_multi_subject", qid, message, subject, facet, s3, "c1"),
        SelectionScenario(f"{qid}_4_stale_vs_current", qid, message, subject, facet, s4, "c1"),
        SelectionScenario(f"{qid}_5_no_relevant_fact", qid, message, subject, facet, s5, None),
    ]


def build_all_scenarios() -> list[SelectionScenario]:
    scenarios: list[SelectionScenario] = []
    scenarios += _build_scenarios(
        "lme-830ce83f", _830CE83F_MSG, "Rachel", "Housing",
        _830CE83F_CANDIDATES_S1, _830CE83F_CANDIDATES_S2, _830CE83F_CANDIDATES_S3,
        _830CE83F_CANDIDATES_S4, _830CE83F_CANDIDATES_S5,
    )
    scenarios += _build_scenarios(
        "lme-852ce960", _852CE960_MSG, "user", "Mortgage",
        _852CE960_CANDIDATES_S1, _852CE960_CANDIDATES_S2, _852CE960_CANDIDATES_S3,
        _852CE960_CANDIDATES_S4, _852CE960_CANDIDATES_S5,
    )
    scenarios += _build_scenarios(
        "lme-2698e78f", _2698E78F_MSG, "Dr. Smith", "Therapy schedule",
        _2698E78F_CANDIDATES_S1, _2698E78F_CANDIDATES_S2, _2698E78F_CANDIDATES_S3,
        _2698E78F_CANDIDATES_S4, _2698E78F_CANDIDATES_S5,
    )
    return scenarios


# ---------------------------------------------------------------------------
# The selector itself
# ---------------------------------------------------------------------------

_SELECTOR_SYSTEM_PROMPT = """You are a context-selection component for a conversational
memory system. You are given a CURRENT MESSAGE and a list of CANDIDATE FACTS from prior
conversation. Your job is to select AT MOST ONE candidate fact that gives useful PRIOR
context for correctly interpreting the CURRENT MESSAGE -- including recognizing when the
CURRENT MESSAGE is UPDATING, CORRECTING, or CONTRADICTING that prior fact.

IMPORTANT: an apparent disagreement between a candidate and the CURRENT MESSAGE is NOT a
reason to reject the candidate -- it is usually exactly the situation this prior context
is needed for. For example, if the CURRENT MESSAGE says someone moved to a new place and
a candidate states where they lived before, that candidate is the correct selection
precisely because the CURRENT MESSAGE is changing it. Do not judge a candidate's
relevance by whether it still matches or agrees with the CURRENT MESSAGE.

Rules:
- Select a candidate only if it is about the SAME subject/entity the CURRENT MESSAGE is
  actually discussing, and the SAME topic area (facet) as what the CURRENT MESSAGE is
  about -- regardless of whether the candidate's specific value/state agrees with the
  CURRENT MESSAGE or is being updated by it.
- If multiple candidates are about the right subject and facet but represent different
  points in time RELATIVE TO EACH OTHER (one candidate has been superseded by a more
  recent one, both from BEFORE the current message), select only the more recent one
  among the candidates. Do not use the CURRENT MESSAGE itself to judge which candidate is
  "more current" -- the CURRENT MESSAGE is very likely a further update beyond all of
  them, and that is expected, not a reason to reject the most recent candidate.
- Do NOT select a candidate just because it shares surface-level vocabulary or a general
  topic (e.g. "housing," "moving") if it is about a DIFFERENT person or entity than the
  CURRENT MESSAGE is discussing.
- If no candidate is about the right subject AND right facet at all, select NOTHING.
  Selecting nothing is the correct answer when no candidate matches; do not guess or pick
  the closest-sounding option out of the available choices just because something in the
  list is topically adjacent.

Return JSON only: {"selected_id": "<candidate id>" or null, "reasoning": "brief"}"""


def _selector_prompt(message: str, candidates: tuple[CandidateFact, ...]) -> str:
    candidate_lines = "\n".join(
        f'- id="{c.id}": subject="{c.subject}", facet="{c.facet}": {c.text}'
        for c in candidates
    )
    return (
        f"CURRENT MESSAGE:\n{message}\n\n"
        f"CANDIDATE FACTS:\n{candidate_lines}\n\n"
        'Return JSON only, e.g. {"selected_id": "c1", "reasoning": "..."} '
        'or {"selected_id": null, "reasoning": "..."}'
    )


def _parse_selector_response(raw: str) -> tuple[str | None, str]:
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
        raise ValueError(f"selected_id must be a string or null, got {type(selected_id)}")
    reasoning = str(parsed.get("reasoning") or "")
    return (selected_id, reasoning)


async def run_selector(llm_backend: object, scenario: SelectionScenario) -> dict:
    """Run the selector against one scenario. Returns a result dict with the selector's
    choice, whether it matched the scenario's ground-truth correct choice, and whether
    the choice (if any) was a decoy (false-grounding indicator)."""
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured selector LLM backend has no create_chat_completion")

    raw = await create(
        system_prompt=_SELECTOR_SYSTEM_PROMPT,
        user_prompt=_selector_prompt(scenario.current_message, scenario.candidates),
        operation="extraction_lab_context_selector",
        max_tokens=300,
        temperature=0.0,
    )
    selected_id, reasoning = _parse_selector_response(raw)

    valid_ids = {c.id for c in scenario.candidates}
    if selected_id is not None and selected_id not in valid_ids:
        # Hallucinated an id not in the pool -- treat as an incorrect, non-null selection
        # (a real failure mode worth counting, not silently dropping to None).
        selected_id = f"INVALID:{selected_id}"

    selected_candidate = next((c for c in scenario.candidates if c.id == selected_id), None)
    correct = selected_id == scenario.correct_candidate_id
    is_decoy_selection = selected_candidate is not None and selected_candidate.decoy_type != DecoyType.NONE

    return {
        "scenario_id": scenario.scenario_id,
        "fixture_qid": scenario.fixture_qid,
        "selected_id": selected_id,
        "correct_candidate_id": scenario.correct_candidate_id,
        "correct": correct,
        "is_decoy_selection": is_decoy_selection,
        "decoy_type": selected_candidate.decoy_type.value if selected_candidate else None,
        "reasoning": reasoning,
    }
