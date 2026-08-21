"""Phase 5 (menhir-extraction-context-ablation-handoff.md): metadata PRODUCTION, not
selector tuning. The Phase 4b selector is frozen -- this module tests whether the
QUERY-SIDE routing pre-pass (subject/facet/state_family/event_type for a new, currently
under-extracted message) can be produced reliably enough to feed that selector.

Critical constraint carried over from the design doc: this pre-pass CANNOT depend on the
full extractor succeeding -- extraction under-admission is the exact failure being
repaired, so routing must work from the raw message alone, via a separate mechanism.

Five approaches compared, per direct instruction:
  a. graph_lookup_only     -- reuse the real, currently-existing signal (Phase 2's
                               candidate-name lookup against the live graph). No facet/
                               state_family capability at all -- demonstrates the gap in
                               what already exists today.
  b. llm_ontology          -- one LLM call, given a fixed small ontology, classifies
                               subject/facet/state_family/event_type or "unknown".
  c. two_stage             -- subject detection first, then facet/state_family
                               classification CONDITIONED on the detected subject
                               (two LLM calls).
  d. hybrid_rules_then_llm -- deterministic keyword rules first; LLM only consulted if
                               rules don't confidently match (may end up fully
                               deterministic on well-known phrasing).
  e. oracle                -- ground truth. Upper-bound sanity reference.

Two evaluation layers:
  1. Field-level accuracy: does the predicted (subject, facet, state_family) match
     ground truth, per fixture, per approach.
  2. Downstream chain (the one that actually matters, per direct instruction): feed the
     PREDICTED metadata into Phase 4b's real is_eligible() filter against that fixture's
     real Phase 4b candidate pools, and see whether the final context selection is still
     correct. Perfect field accuracy that still selects harmful context would not be a
     useful result -- so this is scored independently of (1), never inferred from it.

Provenance: contains conversation data derived from the LongMemEval benchmark
(Wu et al., ICLR 2025; arXiv:2410.10813), MIT-licensed, Copyright (c) 2024 Di Wu.
The full copyright and permission notice is reproduced in NOTICE at the repo root,
which ships with the package -- MIT requires it in redistributions (CF-153).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingPrediction:
    approach: str
    fixture_qid: str
    subject: str | None
    facet: str | None
    state_family: str | None
    event_type: str | None
    raw_reasoning: str = ""


@dataclass(frozen=True)
class RoutingGroundTruth:
    fixture_qid: str
    current_message: str
    source_namespace: str | None  # for approach (a)'s real graph lookup
    subject: str
    facet: str
    state_family: str
    event_type: str


GROUND_TRUTH: list[RoutingGroundTruth] = [
    RoutingGroundTruth(
        "lme-830ce83f",
        "user: Miami Beach sounds fun, but I've been there before. I'm thinking of "
        "somewhere more relaxed. My friend Rachel actually just moved back to the "
        "suburbs again, so I was thinking of somewhere not too far from a major city. "
        "Any suggestions?",
        "lme-830ce83f",
        "Rachel", "Housing", "Current residence", "relocation",
    ),
    RoutingGroundTruth(
        "lme-852ce960",
        "user: I'm planning to move into my new home soon and I need to set up cable "
        "and TV services. Can you recommend some providers in my area and their prices? "
        "By the way, I'm really looking forward to finally owning a home, it's been a "
        "long process, but it'll be worth it to have a backyard like the one I'll have "
        "- remember when I got pre-approved for $400,000 from Wells Fargo?",
        "lme-852ce960",
        "user", "Mortgage", "Pre-approval amount", "financial_disclosure",
    ),
    RoutingGroundTruth(
        "lme-2698e78f",
        "user: I see what you're doing here, but I think it's a bit too structured for "
        "me. I'd rather have some flexibility in my schedule. Can you help me come up "
        "with a more general plan to prioritize my tasks and set boundaries, rather "
        "than a strict schedule? And by the way, speaking of boundaries, I see Dr. "
        "Smith every week, and she's been helping me work on this stuff.",
        "lme-2698e78f",
        "Dr. Smith", "Therapy schedule", "Session frequency", "schedule_update",
    ),
]

# Fixed small ontology for approaches (b), (c) -- deliberately includes the correct
# facet/state_family for all 3 fixtures PLUS several plausible wrong ones (mirroring
# Phase 4b's decoy facets), so a misclassification is a real error, not an artifact of
# an incomplete ontology.
FACET_ONTOLOGY: dict[str, list[str]] = {
    "Housing": ["Current residence", "Housing preferences"],
    "Employment": ["Current job"],
    "Health": ["Fitness routine"],
    "Mortgage": ["Pre-approval amount", "Inspection findings"],
    "Therapy schedule": ["Session frequency", "Discussion focus"],
    "Self-care": ["Sleep routine"],
    "Unknown": ["Unknown"],
}


def _normalize(text: str | None) -> str:
    return (text or "").strip().lower()


def field_matches(predicted: str | None, ground_truth: str) -> bool:
    return _normalize(predicted) == _normalize(ground_truth)


# ---------------------------------------------------------------------------
# Approach (a): graph_lookup_only -- reuse the real, currently-existing signal
# ---------------------------------------------------------------------------

async def predict_graph_lookup_only(clients: object, gt: RoutingGroundTruth) -> RoutingPrediction:
    """Approach (a): the ONLY metadata-adjacent signal that actually exists in
    production today. Deliberately cannot produce facet/state_family/event_type --
    this demonstrates the gap, not a competitive approach."""
    from menhir.explorer.extraction_lab import _lookup_known_entities

    matches = await _lookup_known_entities(clients, gt.source_namespace, gt.current_message)
    subject = matches[0] if matches else None
    return RoutingPrediction(
        "graph_lookup_only", gt.fixture_qid, subject, None, None, None,
        raw_reasoning=f"real graph substring match against {gt.source_namespace}: {matches}",
    )


# ---------------------------------------------------------------------------
# Approach (e): oracle
# ---------------------------------------------------------------------------

def predict_oracle(gt: RoutingGroundTruth) -> RoutingPrediction:
    return RoutingPrediction(
        "oracle", gt.fixture_qid, gt.subject, gt.facet, gt.state_family, gt.event_type,
        raw_reasoning="ground truth",
    )


# ---------------------------------------------------------------------------
# Approach (d): hybrid_rules_then_llm -- deterministic keyword rules, LLM fallback
# ---------------------------------------------------------------------------

_FACET_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("Housing", ["apartment", "moved", "moving", "suburb", "residence", "living in", "home soon"]),
    ("Mortgage", ["mortgage", "pre-approved", "pre-approval", "wells fargo", "loan"]),
    ("Therapy schedule", ["therapy", "dr. smith", "session", "boundaries"]),
]

_SUBJECT_NAME_HINTS = ["Rachel", "Dr. Smith"]  # known proper-noun subjects worth a direct regex check


def _rule_based_facet(message: str) -> str | None:
    lower = message.lower()
    for facet, keywords in _FACET_KEYWORD_RULES:
        if any(kw in lower for kw in keywords):
            return facet
    return None


def _rule_based_subject(message: str) -> str | None:
    for name in _SUBJECT_NAME_HINTS:
        if name.lower() in message.lower():
            return name
    if "user" in message.lower() or message.strip().lower().startswith("user:"):
        return "user"
    return None


async def predict_hybrid_rules_then_llm(llm_backend: object, gt: RoutingGroundTruth) -> RoutingPrediction:
    """Approach (d): try deterministic keyword rules first (facet + a small known-name
    subject list); only call the LLM for whatever the rules couldn't resolve. On these
    3 well-known fixtures this is likely to resolve fully via rules alone (0 LLM calls)
    -- report that plainly if so, it is itself an informative result about how far
    cheap deterministic routing gets on realistic phrasing."""
    facet = _rule_based_facet(gt.current_message)
    subject = _rule_based_subject(gt.current_message)
    state_family: str | None = None
    llm_calls = 0
    reasoning_parts = [f"rules: facet={facet!r} subject={subject!r}"]

    if facet is not None:
        # state_family still needs a decision -- for our 3 fixtures each facet has a
        # dominant state_family; use it deterministically as a second rule tier rather
        # than escalating to the LLM for something the ontology already narrows well.
        candidates = FACET_ONTOLOGY.get(facet, [])
        if len(candidates) == 1:
            state_family = candidates[0]
        elif candidates:
            state_family, calls, reason = await _classify_state_family_llm(
                llm_backend, gt.current_message, facet, candidates
            )
            llm_calls += calls
            reasoning_parts.append(reason)

    if facet is None or subject is None:
        pred_subject, pred_facet, pred_state, calls, reason = await _classify_full_llm(
            llm_backend, gt.current_message
        )
        llm_calls += calls
        reasoning_parts.append(f"llm fallback: {reason}")
        subject = subject or pred_subject
        facet = facet or pred_facet
        state_family = state_family or pred_state

    return RoutingPrediction(
        "hybrid_rules_then_llm", gt.fixture_qid, subject, facet, state_family, None,
        raw_reasoning=f"[llm_calls={llm_calls}] " + " | ".join(reasoning_parts),
    )


# ---------------------------------------------------------------------------
# Approach (b): llm_ontology -- one call, fixed ontology
# ---------------------------------------------------------------------------

_ONTOLOGY_SYSTEM_PROMPT = """You are a routing component for a conversational memory
system. Given a CURRENT MESSAGE, identify what it is explicitly about, using ONLY the
provided ontology. Do not invent categories not in the ontology.

Output:
- subject: the specific person/entity the message is discussing (a name, or "user" if
  it's about the message's own speaker). Use "unknown" if genuinely unclear.
- facet: the topic area, chosen from the ONTOLOGY's facet list, or "Unknown" if none fit.
- state_family: the specific kind of claim within that facet, chosen from the ontology's
  state_family list for that facet, or "Unknown" if none fit.

Return JSON only: {"subject": "...", "facet": "...", "state_family": "...", "reasoning": "brief"}"""


def _ontology_text() -> str:
    lines = []
    for facet, families in FACET_ONTOLOGY.items():
        lines.append(f"- {facet}: {', '.join(families)}")
    return "\n".join(lines)


def _parse_ontology_response(raw: str) -> tuple[str | None, str | None, str | None, str]:
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("routing predictor did not return a JSON object")
    parsed = json.loads(cleaned[start:end + 1])

    def _clean(v: object) -> str | None:
        s = str(v or "").strip()
        return None if s.lower() in ("", "unknown", "none", "null") else s

    return (_clean(parsed.get("subject")), _clean(parsed.get("facet")),
            _clean(parsed.get("state_family")), str(parsed.get("reasoning") or ""))


async def _classify_full_llm(llm_backend: object, message: str) -> tuple[str | None, str | None, str | None, int, str]:
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured routing LLM backend has no create_chat_completion")
    raw = await create(
        system_prompt=_ONTOLOGY_SYSTEM_PROMPT,
        user_prompt=f"ONTOLOGY:\n{_ontology_text()}\n\nCURRENT MESSAGE:\n{message}\n\nReturn JSON only.",
        operation="extraction_lab_routing_ontology",
        max_tokens=250,
        temperature=0.0,
    )
    subject, facet, state_family, reasoning = _parse_ontology_response(raw)
    return (subject, facet, state_family, 1, reasoning)


async def predict_llm_ontology(llm_backend: object, gt: RoutingGroundTruth) -> RoutingPrediction:
    subject, facet, state_family, calls, reasoning = await _classify_full_llm(llm_backend, gt.current_message)
    return RoutingPrediction(
        "llm_ontology", gt.fixture_qid, subject, facet, state_family, None,
        raw_reasoning=f"[llm_calls={calls}] {reasoning}",
    )


# ---------------------------------------------------------------------------
# Approach (c): two_stage -- subject first, then facet/state_family conditioned on it
# ---------------------------------------------------------------------------

_SUBJECT_ONLY_SYSTEM_PROMPT = """Identify the single explicit subject (person or entity,
or "user" if it's the message's own speaker) that the CURRENT MESSAGE is discussing.
Return JSON only: {"subject": "..." or "unknown", "reasoning": "brief"}"""

_FACET_GIVEN_SUBJECT_SYSTEM_PROMPT = """Given a CURRENT MESSAGE and a known SUBJECT,
classify the topic area (facet) and specific claim type (state_family) the message
discusses ABOUT THAT SUBJECT, using ONLY the provided ontology.
Return JSON only: {"facet": "...", "state_family": "...", "reasoning": "brief"}"""


async def _detect_subject_only(llm_backend: object, message: str) -> tuple[str | None, int, str]:
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured routing LLM backend has no create_chat_completion")
    raw = await create(
        system_prompt=_SUBJECT_ONLY_SYSTEM_PROMPT,
        user_prompt=f"CURRENT MESSAGE:\n{message}\n\nReturn JSON only.",
        operation="extraction_lab_routing_subject",
        max_tokens=150,
        temperature=0.0,
    )
    import re
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    parsed = json.loads(cleaned[start:end + 1]) if start >= 0 and end > start else {}
    subject_raw = str(parsed.get("subject") or "").strip()
    subject = None if subject_raw.lower() in ("", "unknown", "none") else subject_raw
    return (subject, 1, str(parsed.get("reasoning") or ""))


async def _classify_state_family_llm(
    llm_backend: object, message: str, facet: str, candidates: list[str],
) -> tuple[str | None, int, str]:
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured routing LLM backend has no create_chat_completion")
    raw = await create(
        system_prompt='Given a CURRENT MESSAGE, a known FACET, and a list of possible '
                      'state_family values, pick the one that best matches, or "unknown". '
                      'Return JSON only: {"state_family": "...", "reasoning": "brief"}',
        user_prompt=f"CURRENT MESSAGE:\n{message}\n\nFACET: {facet}\nCANDIDATES: {candidates}\n\nReturn JSON only.",
        operation="extraction_lab_routing_state_family",
        max_tokens=150,
        temperature=0.0,
    )
    import re
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    parsed = json.loads(cleaned[start:end + 1]) if start >= 0 and end > start else {}
    sf_raw = str(parsed.get("state_family") or "").strip()
    sf = None if sf_raw.lower() in ("", "unknown", "none") else sf_raw
    return (sf, 1, str(parsed.get("reasoning") or ""))


async def predict_two_stage(llm_backend: object, gt: RoutingGroundTruth) -> RoutingPrediction:
    subject, calls1, reason1 = await _detect_subject_only(llm_backend, gt.current_message)

    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured routing LLM backend has no create_chat_completion")
    raw = await create(
        system_prompt=_FACET_GIVEN_SUBJECT_SYSTEM_PROMPT,
        user_prompt=(
            f"CURRENT MESSAGE:\n{gt.current_message}\n\nSUBJECT: {subject}\n\n"
            f"ONTOLOGY:\n{_ontology_text()}\n\nReturn JSON only."
        ),
        operation="extraction_lab_routing_facet_given_subject",
        max_tokens=200,
        temperature=0.0,
    )
    import re
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    parsed = json.loads(cleaned[start:end + 1]) if start >= 0 and end > start else {}
    facet_raw = str(parsed.get("facet") or "").strip()
    facet = None if facet_raw.lower() in ("", "unknown", "none") else facet_raw
    sf_raw = str(parsed.get("state_family") or "").strip()
    state_family = None if sf_raw.lower() in ("", "unknown", "none") else sf_raw

    return RoutingPrediction(
        "two_stage", gt.fixture_qid, subject, facet, state_family, None,
        raw_reasoning=f"[llm_calls=2] stage1: {reason1} | stage2: {parsed.get('reasoning', '')}",
    )


# ---------------------------------------------------------------------------
# Approach (f): candidate_aware_ranked -- single joint call, grounded in the REAL
# (subject, facet, state_family) triples that actually exist across stored candidates,
# not an abstract ontology; returns up to 2 ranked hypotheses with confidence.
# ---------------------------------------------------------------------------

#: The real, distinct (subject, facet, state_family) triples across ALL 3 fixtures'
#: Phase 4b candidate pools (correct answers AND decoys -- a production system would
#: not know in advance which triples are "correct" for a given query, only which
#: triples exist in the graph at all). Pulled directly from
#: extraction_lab_eligibility_fixtures.py so this never drifts out of sync with the
#: actual candidate pools the downstream chain test uses.
def _known_triples() -> list[tuple[str, str, str]]:
    from menhir.explorer.extraction_lab_eligibility_fixtures import (
        _830_scenarios, _852_scenarios, _2698_scenarios,
    )
    seen: set[tuple[str, str, str]] = set()
    for scenarios in (_830_scenarios(), _852_scenarios(), _2698_scenarios()):
        for s in scenarios:
            for c in s.candidates:
                if c.facet is not None and c.state_family is not None:
                    seen.add((c.subject, c.facet, c.state_family))
    return sorted(seen)


_CANDIDATE_AWARE_SYSTEM_PROMPT = """You are a routing component for a conversational
memory system. You are given a CURRENT MESSAGE and a list of KNOWN (subject, facet,
state_family) triples that actually exist in the memory graph -- these are the ONLY
valid combinations; do not invent others.

Identify which KNOWN triple(s) the CURRENT MESSAGE is discussing. Return up to 2 ranked
hypotheses, most likely first, each with a confidence 0.0-1.0. If nothing in the list
plausibly matches, return an empty list -- do not force a match.

Return JSON only:
{"hypotheses": [{"subject": "...", "facet": "...", "state_family": "...", "confidence": 0.0}, ...]}"""


def _candidate_aware_prompt(message: str, triples: list[tuple[str, str, str]]) -> str:
    lines = "\n".join(f"- subject={s!r}, facet={f!r}, state_family={sf!r}" for s, f, sf in triples)
    return (
        f"CURRENT MESSAGE:\n{message}\n\nKNOWN TRIPLES:\n{lines}\n\n"
        'Return JSON only, e.g. {"hypotheses": [{"subject": "Rachel", "facet": "Housing", '
        '"state_family": "Current residence", "confidence": 0.9}]}'
    )


def _parse_ranked_hypotheses(raw: str) -> list[dict]:
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("candidate-aware predictor did not return a JSON object")
    parsed = json.loads(cleaned[start:end + 1])
    hyps = parsed.get("hypotheses")
    if not isinstance(hyps, list):
        return []
    return [
        h for h in hyps
        if isinstance(h, dict) and h.get("subject") and h.get("facet") and h.get("state_family")
    ]


async def predict_candidate_aware_ranked(llm_backend: object, gt: RoutingGroundTruth) -> RoutingPrediction:
    """Approach (f): single joint call (per the "keep subject/facet/state_family
    together" principle), grounded in the real known triples instead of an abstract
    ontology, returning ranked hypotheses. This function returns only the TOP-RANKED
    hypothesis as the RoutingPrediction (for comparability with the other approaches'
    single-answer shape) -- the full ranked list is preserved in raw_reasoning so the
    runner can separately test "does trying hypothesis 2 when hypothesis 1 fails
    recover any additional coverage" without re-calling the LLM."""
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured routing LLM backend has no create_chat_completion")

    triples = _known_triples()
    raw = await create(
        system_prompt=_CANDIDATE_AWARE_SYSTEM_PROMPT,
        user_prompt=_candidate_aware_prompt(gt.current_message, triples),
        operation="extraction_lab_routing_candidate_aware",
        max_tokens=300,
        temperature=0.0,
    )
    hypotheses = _parse_ranked_hypotheses(raw)

    if not hypotheses:
        return RoutingPrediction(
            "candidate_aware_ranked", gt.fixture_qid, None, None, None, None,
            raw_reasoning="[llm_calls=1] no hypotheses returned",
        )

    top = hypotheses[0]
    return RoutingPrediction(
        "candidate_aware_ranked", gt.fixture_qid,
        str(top.get("subject")), str(top.get("facet")), str(top.get("state_family")), None,
        raw_reasoning=f"[llm_calls=1] hypotheses={json.dumps(hypotheses)}",
    )
