"""Pure tests for the belief-evidence assembly adapter (belief-gate producer)."""

from __future__ import annotations

from menhir.domain.belief import EvidencePolarity, EvidenceSignal
from menhir.domain.belief_evidence import assemble_belief_evidence, score_candidate_belief


def test_no_temporal_signal_yields_no_score() -> None:
    # ordinary memory: no belief marker -> None (warden stays permissive)
    assert score_candidate_belief("u1", "some memory", {"evidence_kinds": ("graphiti",)}) is None


def test_superseded_emits_is_expired_contradicts() -> None:
    ev = assemble_belief_evidence({"belief_superseded": True, "evidence_kinds": ("git",)})
    sigs = {(e.signal, e.polarity) for e in ev}
    assert (EvidenceSignal.IS_EXPIRED, EvidencePolarity.CONTRADICTS) in sigs
    assert (EvidenceSignal.SOURCE_IS_GIT, EvidencePolarity.SUPPORTS) in sigs


def test_current_temporal_emits_valid_at_supports() -> None:
    ev = assemble_belief_evidence({"belief_has_temporal": True})
    assert ev[0].signal is EvidenceSignal.IS_VALID_AT_QUERY_TIME
    assert ev[0].polarity is EvidencePolarity.SUPPORTS


def test_superseded_candidate_scores_and_buckets() -> None:
    score = score_candidate_belief("u1", "the patch fixed it", {"belief_superseded": True})
    assert score is not None
    # a superseded candidate must not score as plainly safe-to-assert
    assert score.bucket.name != "SAFE_TO_ASSERT"


def test_unknown_evidence_kinds_are_ignored() -> None:
    ev = assemble_belief_evidence({"belief_has_temporal": True, "evidence_kinds": ("bogus",)})
    assert all(e.note != "provenance:bogus" for e in ev)


def test_staleness_evidence_is_included_in_assembly() -> None:
    staleness_ev = [
        EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS,
        "git: file changed after belief"
    ]
    ev_obj = EvidenceSignal(staleness_ev[0])  # type: ignore
    from menhir.domain.belief import BeliefEvidence as BE
    stale_ev = BE(ev_obj, EvidencePolarity.CONTRADICTS, note="git: file changed after belief")
    metadata = {"staleness_evidence": (stale_ev,)}
    ev = assemble_belief_evidence(metadata)
    assert any(e.signal == EvidenceSignal.LATER_CONTRADICTED for e in ev)


def test_score_candidate_belief_with_staleness_evidence() -> None:
    from menhir.domain.belief import BeliefEvidence as BE
    stale_ev = BE(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS,
                  note="git: file changed after belief")
    metadata = {"staleness_evidence": (stale_ev,)}
    score = score_candidate_belief("u1", "code belief", metadata)
    assert score is not None
    # a stale candidate (LATER_CONTRADICTED) must not score as plainly safe-to-assert
    assert score.bucket.name != "SAFE_TO_ASSERT"
