"""Assemble BeliefEvidence + a BeliefScore for a recalled candidate from its metadata.

Pure adapter between menhir's recall-candidate metadata (provenance evidence_kinds + bi-
temporal markers) and the belief-scoring model (belief.py). No I/O, no graph. Returns None
when the candidate carries no belief-relevant signal, so CurrentnessWarden stays permissive
(ADMIT) on ordinary memories and acts only on belief-bearing ones.
"""

from __future__ import annotations

from menhir.domain.belief import (
    BeliefCandidate,
    BeliefCandidateType,
    BeliefEvidence,
    BeliefScore,
    BeliefScorer,
    EvidencePolarity,
    EvidenceSignal,
)
# KIND_TO_SIGNAL is the SSOT in domain/truth/kinds.py; imported here under a private
# alias to preserve the existing call sites inside this module unchanged.
from menhir.domain.truth.kinds import KIND_TO_SIGNAL as _KIND_TO_SIGNAL

_SCORER = BeliefScorer()


def assemble_belief_evidence(metadata: dict[str, object]) -> tuple[BeliefEvidence, ...]:
    """Map a candidate's metadata signals to BeliefEvidence. Pure, order-stable.

    Temporal: belief_superseded -> IS_EXPIRED (CONTRADICTS); else belief_has_temporal ->
    IS_VALID_AT_QUERY_TIME (SUPPORTS). Provenance: each known evidence_kind -> its anchor
    signal (SUPPORTS). Staleness: pre-computed git staleness evidence from derive_structural_staleness.
    strength=1.0; the scorer's DEFAULT_SIGNAL_WEIGHTS carry the weight.
    """
    ev: list[BeliefEvidence] = []
    if metadata.get("belief_superseded"):
        ev.append(BeliefEvidence(
            EvidenceSignal.IS_EXPIRED, EvidencePolarity.CONTRADICTS, 1.0,
            "superseded fact", "temporal"))
    elif metadata.get("belief_has_temporal"):
        ev.append(BeliefEvidence(
            EvidenceSignal.IS_VALID_AT_QUERY_TIME, EvidencePolarity.SUPPORTS, 1.0,
            "current fact", "temporal"))
    for kind in metadata.get("evidence_kinds", ()) or ():
        sig = _KIND_TO_SIGNAL.get(str(kind))
        if sig is not None:
            ev.append(BeliefEvidence(
                sig, EvidencePolarity.SUPPORTS, 1.0, f"provenance:{kind}", "graph"))
    for ev_obj in metadata.get("staleness_evidence", ()) or ():
        if isinstance(ev_obj, BeliefEvidence):
            ev.append(ev_obj)
    return tuple(ev)


def score_candidate_belief(
    candidate_id: str, content: str, metadata: dict[str, object]
) -> BeliefScore | None:
    """Score a candidate's belief state, or None when it carries no belief-relevant signal.

    None unless a temporal marker or staleness evidence is present, so CurrentnessWarden stays
    permissive on ordinary memories. candidate_type: SUPERSESSION when superseded, else
    DEPENDENCY_STATE.
    """
    has_signal = (metadata.get("belief_superseded") or metadata.get("belief_has_temporal")
                  or bool(metadata.get("staleness_evidence")))
    if not has_signal:
        return None
    ctype = (
        BeliefCandidateType.SUPERSESSION
        if metadata.get("belief_superseded")
        else BeliefCandidateType.DEPENDENCY_STATE
    )
    touched = tuple(str(e) for e in (metadata.get("touched_entities", ()) or ()))
    candidate = BeliefCandidate(
        id=candidate_id, statement=content[:200], candidate_type=ctype, touched_entities=touched
    )
    return _SCORER.score(candidate, assemble_belief_evidence(metadata))
