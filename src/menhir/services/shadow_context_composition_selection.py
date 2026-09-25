"""Grounded-response parsing and eligibility selection for Stage 1 shadow composition.

Extracted from shadow_context_composition.py (the Stage 1 facade, which re-exports
what its entry points call): the LLM payload/JSON helpers, the deterministic
eligibility filter, and the LLM tie-break for genuine ties.
"""

from __future__ import annotations

import json
import re

from menhir.domain.temporal import FactTemporal, TemporalQuery, matches_query
from menhir.services.shadow_context_composition_models import (
    _STATUS_ABSTAINED_NO_ELIGIBLE,
    _STATUS_ABSTAINED_TIE,
    ShadowCandidateFact,
    ShadowCandidateLabels,
    ShadowRankedHypothesis,
    ShadowRejection,
)


def _candidate_payload(c: ShadowCandidateFact) -> dict[str, str]:
    return {
        "fact_uuid": c.fact_uuid, "fact_text": c.fact_text,
        "source_name": c.source_name, "target_name": c.target_name,
    }


def _extract_json_object(raw: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    return json.loads(cleaned[start:end + 1])


def _parse_shadow_grounded_response(
    raw: str,
) -> tuple[list[ShadowRankedHypothesis], list[ShadowCandidateLabels]]:
    parsed = _extract_json_object(raw)

    hyps_raw = parsed.get("message_hypotheses")
    hypotheses: list[ShadowRankedHypothesis] = []
    if isinstance(hyps_raw, list):
        for h in hyps_raw:
            if not isinstance(h, dict):
                continue
            facet, family = h.get("shadow_facet"), h.get("shadow_state_family")
            if facet and family:
                try:
                    confidence = float(h.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                hypotheses.append(ShadowRankedHypothesis(str(facet), str(family), confidence))

    labels_raw = parsed.get("candidate_labels")
    labels: list[ShadowCandidateLabels] = []
    if isinstance(labels_raw, list):
        for entry in labels_raw:
            if not isinstance(entry, dict) or not entry.get("fact_uuid"):
                continue
            labels.append(ShadowCandidateLabels(
                fact_uuid=str(entry["fact_uuid"]),
                shadow_facet=(str(entry["shadow_facet"]) if entry.get("shadow_facet") else None),
                shadow_state_family=(str(entry["shadow_state_family"]) if entry.get("shadow_state_family") else None),
                shadow_scope=(str(entry["shadow_scope"]) if entry.get("shadow_scope") else None),
            ))

    if not hypotheses and not labels:
        raise ValueError("LLM response had neither message_hypotheses nor candidate_labels")
    return hypotheses, labels


def _label_matches_hypothesis(label: ShadowCandidateLabels, hyp: ShadowRankedHypothesis) -> bool:
    if label.shadow_facet is None or label.shadow_state_family is None:
        return False
    return (
        label.shadow_facet.strip().lower() == hyp.shadow_facet.strip().lower()
        and label.shadow_state_family.strip().lower() == hyp.shadow_state_family.strip().lower()
    )


async def _select_eligible_candidate(
    llm: object,
    *,
    episode_body: str,
    reference_time: str,
    candidates: list[ShadowCandidateFact],
    message_hypotheses: list[ShadowRankedHypothesis],
    candidate_labels: list[ShadowCandidateLabels],
) -> tuple[str | None, str | None, bool, list[ShadowRejection]]:
    """Deterministic eligibility filter (temporal + shadow-label match against the top
    hypothesis), LLM tie-break only when 2+ candidates survive. Returns
    (selected_fact_uuid, abstention_reason, llm_tie_breaker_fired, rejected)."""
    rejected: list[ShadowRejection] = []

    if not message_hypotheses:
        for c in candidates:
            rejected.append(ShadowRejection(c.fact_uuid, "no_message_hypothesis"))
        return None, _STATUS_ABSTAINED_NO_ELIGIBLE, False, rejected

    top_hypothesis = message_hypotheses[0]
    labels_by_uuid = {label.fact_uuid: label for label in candidate_labels}

    survivors: list[ShadowCandidateFact] = []
    for c in candidates:
        temporal = FactTemporal(
            valid_at=c.valid_at, invalid_at=c.invalid_at,
            created_at=c.created_at, expired_at=c.expired_at,
        )
        if not matches_query(temporal, TemporalQuery.AS_KNOWN_AT, as_of=reference_time):
            rejected.append(ShadowRejection(c.fact_uuid, "not_known_at_reference_time"))
            continue
        label = labels_by_uuid.get(c.fact_uuid)
        if label is None:
            rejected.append(ShadowRejection(c.fact_uuid, "no_matching_label"))
            continue
        if not _label_matches_hypothesis(label, top_hypothesis):
            rejected.append(ShadowRejection(c.fact_uuid, "shadow_label_mismatch"))
            continue
        survivors.append(c)

    if not survivors:
        return None, _STATUS_ABSTAINED_NO_ELIGIBLE, False, rejected
    if len(survivors) == 1:
        return survivors[0].fact_uuid, None, False, rejected

    # 2+ survivors: genuine tie -- consult the LLM tie-breaker rather than picking
    # arbitrarily (Phase 5 item 4's finding: the deterministic path always picks
    # SOMETHING under a tie, which is exactly the case this stage must not repeat).
    break_tie = getattr(llm, "break_shadow_tie", None)
    if break_tie is None:
        return None, _STATUS_ABSTAINED_TIE, False, rejected
    try:
        raw = await break_tie(episode_body, [_candidate_payload(c) for c in survivors])
    except Exception:
        return None, _STATUS_ABSTAINED_TIE, True, rejected
    if raw is None:
        return None, _STATUS_ABSTAINED_TIE, True, rejected
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        return None, _STATUS_ABSTAINED_TIE, True, rejected

    picked = parsed.get("selected_fact_uuid")
    survivor_uuids = {c.fact_uuid for c in survivors}
    if picked and str(picked) in survivor_uuids:
        return str(picked), None, True, rejected
    return None, _STATUS_ABSTAINED_TIE, True, rejected
