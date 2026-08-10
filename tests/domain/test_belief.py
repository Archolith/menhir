from menhir.domain.belief import (
    BeliefCandidate,
    BeliefCandidateType,
    BeliefEvidence,
    BeliefHead,
    BeliefScorer,
    EvidencePolarity,
    EvidenceSignal,
    RecallBucket,
)


def test_belief_scorer_preserves_superseded_patch_belief() -> None:
    scorer = BeliefScorer()
    patch_fixed = BeliefCandidate(
        id="belief_patch_fixed",
        statement="The CE willow patch fixed the original texture-cache crash.",
        candidate_type=BeliefCandidateType.FIX,
        touched_entities=("CE_Willow_TextureCache.xml", "TreeWillowLeaflessImmature"),
    )

    after_patch_evidence = [
        BeliefEvidence(
            EvidenceSignal.FILE_CHANGED,
            note="CE willow patch was added to the working tree.",
        ),
        BeliefEvidence(
            EvidenceSignal.TEST_PASSED,
            note="The original crash appeared resolved after the patch.",
        ),
    ]
    after_load_order_evidence = [
        *after_patch_evidence,
        BeliefEvidence(
            EvidenceSignal.LATER_CONTRADICTED,
            EvidencePolarity.CONTRADICTS,
            note="A later compatibility/load-order failure appeared after the patch.",
        ),
    ]

    after_patch_score = scorer.score(patch_fixed, after_patch_evidence)
    after_load_order_score = scorer.score(patch_fixed, after_load_order_evidence)

    assert after_patch_score.probability > 0.75
    assert after_load_order_score.probability < after_patch_score.probability
    assert after_load_order_score.contradiction > 0
    assert after_load_order_score.bucket == RecallBucket.CONFLICT_SET
    assert any("load-order" in reason for reason in after_load_order_score.rationale)


def test_belief_scorer_ranks_later_load_order_cause_above_old_patch_fix() -> None:
    scorer = BeliefScorer()
    patch_fixed = BeliefCandidate(
        id="belief_patch_fixed",
        statement="The CE willow patch fixed the crash.",
        candidate_type=BeliefCandidateType.FIX,
    )
    load_order_cause = BeliefCandidate(
        id="belief_load_order_cause",
        statement="Load order caused the remaining compatibility issue.",
        candidate_type=BeliefCandidateType.CAUSE,
    )

    ranked = scorer.rank(
        [patch_fixed, load_order_cause],
        {
            "belief_patch_fixed": [
                BeliefEvidence(EvidenceSignal.TEST_PASSED),
                BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS),
            ],
            "belief_load_order_cause": [
                BeliefEvidence(EvidenceSignal.OBSERVED_ERROR, note="Compatibility/load-order error observed."),
                BeliefEvidence(EvidenceSignal.LATER_CONFIRMED, note="Load-order fix resolved the issue."),
                BeliefEvidence(EvidenceSignal.SOURCE_IS_LOG),
            ],
        },
    )

    assert ranked[0].candidate.id == "belief_load_order_cause"
    assert ranked[0].probability > ranked[1].probability
    assert ranked[0].bucket == RecallBucket.SAFE_TO_ASSERT


def test_belief_heads_keep_current_truth_and_supersession_separate() -> None:
    scorer = BeliefScorer()
    patch_fully_fixed_issue = BeliefCandidate(
        id="belief_patch_fully_fixed_issue",
        statement="The CE willow patch fully fixed the issue.",
        candidate_type=BeliefCandidateType.FIX,
    )

    current_score = scorer.score(
        patch_fully_fixed_issue,
        [
            BeliefEvidence(EvidenceSignal.IS_EXPIRED, EvidencePolarity.CONTRADICTS),
            BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS),
        ],
        head=BeliefHead.CURRENT,
    )
    superseded_score = scorer.score(
        patch_fully_fixed_issue,
        [
            BeliefEvidence(EvidenceSignal.IS_EXPIRED),
            BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED),
        ],
        head=BeliefHead.SUPERSEDED,
    )

    assert current_score.head == BeliefHead.CURRENT
    assert superseded_score.head == BeliefHead.SUPERSEDED
    assert current_score.probability < 0.5
    assert current_score.bucket == RecallBucket.DO_NOT_ASSERT
    assert superseded_score.probability > 0.85
    assert superseded_score.bucket == RecallBucket.DO_NOT_ASSERT


def test_recall_packet_groups_scores_by_assertion_policy() -> None:
    scorer = BeliefScorer()
    load_order_cause = BeliefCandidate(
        id="belief_load_order_cause",
        statement="Load order caused the remaining compatibility issue.",
        candidate_type=BeliefCandidateType.CAUSE,
    )
    patch_fixed = BeliefCandidate(
        id="belief_patch_fixed",
        statement="The CE willow patch fixed the crash.",
        candidate_type=BeliefCandidateType.FIX,
    )
    stale_fact = BeliefCandidate(
        id="belief_old_patch_fully_fixed",
        statement="The CE willow patch fully fixed all issues.",
        candidate_type=BeliefCandidateType.FIX,
    )

    packet = scorer.build_recall_packet(
        [
            scorer.score(
                load_order_cause,
                [
                    BeliefEvidence(EvidenceSignal.LATER_CONFIRMED),
                    BeliefEvidence(EvidenceSignal.SOURCE_IS_LOG),
                ],
            ),
            scorer.score(
                patch_fixed,
                [
                    BeliefEvidence(EvidenceSignal.TEST_PASSED),
                    BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS),
                ],
            ),
            scorer.score(
                stale_fact,
                [
                    BeliefEvidence(EvidenceSignal.IS_EXPIRED),
                    BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED),
                ],
                head=BeliefHead.SUPERSEDED,
            ),
        ]
    )

    assert [score.candidate.id for score in packet.safe_to_assert] == ["belief_load_order_cause"]
    assert [score.candidate.id for score in packet.conflict_set] == ["belief_patch_fixed"]
    assert [score.candidate.id for score in packet.do_not_assert] == ["belief_old_patch_fully_fixed"]
