"""Typed-scalar k-sample consistency gate (ScalarStateView Piece C.4, commit 2) — offline.

Proves the SOURCE-CLAIM-FIRST voting contract: group k samples by source_key, then vote on the
interpretation (subject + slot + operation + value + when); a sample omitting or internally
conflicting on a claim casts NO vote but stays in the k denominator; the winner is the top REAL
interpretation and commits only at/above threshold. No persistence, no binding. Proposals are
constructed directly (source_key is derived from episode_uuid + span, so identity is controllable),
plus one end-to-end check through the C.4.1 extractor to prove clock canonicalization prevents scatter."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    extract_typed_scalars_once,
    gate_typed_scalars,
)


def _prop(**over) -> TypedScalarProposal:
    base = dict(
        subject_text="user", attribute="wake", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="07:30", stated_span="wake at 07:30",
        episode_uuid="ep-1", span_start=0, span_end=13, when="2026-07-01T00:00:00+00:00",
    )
    base.update(over)
    return TypedScalarProposal(**base)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


# ------------------------------------------------------------------------------- basic structure

@pytest.mark.unit
def test_k_zero_returns_no_decisions():
    assert gate_typed_scalars([]) == []


@pytest.mark.unit
def test_no_proposals_yields_no_decisions():
    assert gate_typed_scalars([[], [], []]) == []


@pytest.mark.unit
def test_unanimous_commits_and_carries_representative():
    p = _prop(value="07:30", attribute="wake")
    out = gate_typed_scalars([[p], [p], [p]])
    assert len(out) == 1
    d = out[0]
    assert d.committed and d.veto == "commit" and d.agreement == 1.0 and d.k == 3
    assert d.reason == "unanimous"
    assert d.proposal is not None and d.proposal.value == "07:30"
    assert d.evidence_tier == "agent"                       # perception is always the lowest tier
    assert sum(d.distribution.values()) == 3               # every sample is in the denominator


@pytest.mark.unit
def test_distinct_source_keys_are_separate_decisions():
    # different spans -> different source_key -> two independent claims, each unanimous.
    a = _prop(attribute="wake", span_start=0, span_end=13)
    b = _prop(attribute="sleep", span_start=20, span_end=33)
    out = gate_typed_scalars([[a, b], [a, b], [a, b]])
    assert len(out) == 2 and all(d.committed for d in out)
    assert {d.source_key for d in out} == {a.source_key, b.source_key}


# ---------------------------------------------------------------------- omissions count against

@pytest.mark.unit
def test_omission_counts_against_agreement():
    # 2 of 5 samples perceive the claim; the other 3 are absent -> agreement 2/5 < 1.0 -> abstain.
    p = _prop()
    out = gate_typed_scalars([[p], [p], [], [], []])
    assert len(out) == 1
    d = out[0]
    assert not d.committed and d.veto == "self_consistency"
    assert abs(d.agreement - 0.4) < 1e-9 and d.k == 5
    assert sum(d.distribution.values()) == 5               # omissions stay in the denominator


# ---------------------------------------------------------- one vote per sample per source claim

@pytest.mark.unit
def test_conflicted_sample_casts_no_vote():
    # sample 0 reads the SAME source span two DIFFERENT ways (two values) -> conflicted -> no vote.
    # samples 1,2 agree on 07:30. A naive double-count would give 07:30 = 3/3; the gate gives 2/3.
    p_730 = _prop(value="07:30", stated_span="wake at 07:30")
    p_830 = _prop(value="08:30", stated_span="wake at 07:30")   # same span => same source_key
    assert p_730.source_key == p_830.source_key
    out = gate_typed_scalars([[p_730, p_830], [p_730], [p_730]])
    assert len(out) == 1
    d = out[0]
    assert not d.committed and d.veto == "self_consistency"
    assert abs(d.agreement - (2 / 3)) < 1e-9                # 2 clean votes, conflicted sample = 0


@pytest.mark.unit
def test_clean_agreement_commits_where_conflict_would_have_blocked():
    # contrast to the above: with sample 0 voting cleanly for 07:30, all three agree -> commit.
    p = _prop(value="07:30")
    out = gate_typed_scalars([[p], [p], [p]])
    assert out[0].committed and out[0].agreement == 1.0


# ---------------------------------------------- subject and when participate in the interpretation

@pytest.mark.unit
def test_same_source_different_subject_does_not_agree():
    # identical span/value/when, but different extracted subject -> two interpretations -> scatter.
    # (proves subject_text is part of the vote; if it were ignored these would agree at 1.0.)
    alice = _prop(subject_text="alice", span_start=0, span_end=13)
    bob = _prop(subject_text="bob", span_start=0, span_end=13)
    assert alice.source_key == bob.source_key
    out = gate_typed_scalars([[alice], [bob]])
    assert len(out) == 1 and not out[0].committed
    assert abs(out[0].agreement - 0.5) < 1e-9              # 1/2 each -> below unanimous


@pytest.mark.unit
def test_same_source_different_when_does_not_agree():
    # identical span/subject/value, different effective date -> not the same assertion -> scatter.
    d1 = _prop(when="2026-07-01T00:00:00+00:00")
    d2 = _prop(when="2026-07-02T00:00:00+00:00")
    out = gate_typed_scalars([[d1], [d2]])
    assert len(out) == 1 and not out[0].committed          # value agrees, world-time disagrees


@pytest.mark.unit
def test_absent_when_is_a_distinct_interpretation_from_a_dated_one():
    dated = _prop(when="2026-07-01T00:00:00+00:00")
    undated = _prop(when=None)
    out = gate_typed_scalars([[dated], [undated]])
    assert len(out) == 1 and not out[0].committed


# ------------------------------------------------------------------------- threshold + determinism

@pytest.mark.unit
def test_threshold_below_one_commits_concentrated():
    x = _prop(value="07:30")
    y = _prop(value="08:30")
    out = gate_typed_scalars([[x], [x], [y]], threshold=0.6)   # 2/3 = 0.667 >= 0.6
    assert len(out) == 1
    d = out[0]
    assert d.committed and d.veto == "commit"
    assert abs(d.agreement - (2 / 3)) < 1e-9 and d.proposal.value == "07:30"


@pytest.mark.unit
def test_representative_is_first_seen_and_preserves_value_type():
    # 37 (int) and 37.0 (float) normalize to the same interpretation, so they AGREE; the committed
    # representative is the FIRST-seen proposal (deterministic), preserving its exact typed value.
    p_int = _prop(attribute="coins", value_kind="count", unit="", value=37, stated_span="37 coins",
                  span_start=0, span_end=8)
    p_float = _prop(attribute="coins", value_kind="count", unit="", value=37.0, stated_span="37 coins",
                    span_start=0, span_end=8)
    assert p_int.source_key == p_float.source_key
    out = gate_typed_scalars([[p_int], [p_float]])
    assert len(out) == 1 and out[0].committed and out[0].agreement == 1.0
    assert out[0].proposal.value == 37 and isinstance(out[0].proposal.value, int)


# ----------------------------------------------------------------------- defense-in-depth grounding

@pytest.mark.unit
def test_ungrounded_winner_is_vetoed():
    # C.4.1 never emits an ungrounded proposal, but if one is injected directly, the gate fails closed.
    p = _prop(span_start=-1, span_end=-1)
    out = gate_typed_scalars([[p], [p], [p]])
    assert len(out) == 1 and not out[0].committed and out[0].veto == "ungrounded"


# ------------------------------------------------------------------------ threshold + unique winner

@pytest.mark.unit
@pytest.mark.parametrize("bad", [0.0, -0.5, 1.5, float("nan"), float("inf"), True, "1", None])
def test_invalid_threshold_is_rejected(bad):
    # a threshold outside (0, 1], non-finite, bool, or non-numeric must FAIL CLOSED, not fail open
    # (agreement < NaN is always False, which would commit a scattered claim).
    p = _prop()
    with pytest.raises(ValueError):
        gate_typed_scalars([[p], [p]], threshold=bad)


@pytest.mark.unit
def test_two_way_tie_abstains_even_when_it_meets_threshold():
    # 2 votes for 07:30, 2 for 08:30, k=4 -> modal count 2/4 = 0.5 meets threshold=0.5, but there is
    # no UNIQUE winner, so the claim must abstain (tie) rather than commit whichever sorted first.
    x = _prop(value="07:30")
    y = _prop(value="08:30")
    out = gate_typed_scalars([[x], [x], [y], [y]], threshold=0.5)
    assert len(out) == 1
    d = out[0]
    assert not d.committed and d.veto == "tie"
    assert abs(d.agreement - 0.5) < 1e-9


@pytest.mark.unit
def test_unique_modal_still_commits_when_threshold_met():
    # contrast: a clear plurality (no tie at the top) commits at the same threshold.
    x = _prop(value="07:30")
    y = _prop(value="08:30")
    out = gate_typed_scalars([[x], [x], [x], [y]], threshold=0.5)   # 3 vs 1 -> unique winner
    assert out[0].committed and out[0].proposal.value == "07:30"


# ------------------------------------------------------------------------------- end-to-end (C.4.1)

@pytest.mark.unit
def test_clock_canonicalization_prevents_scatter_through_extractor():
    # two samples read the SAME span but write "7:30" vs "07:30"; C.4.1 canonicalizes both to 07:30,
    # so grouped by source_key they AGREE and commit — proving canonicalization removes vote scatter.
    ep = [_Ep(uuid="ep-1", content="I wake at 7:30 every day.")]

    def _row(value):
        return dict(episode=0, subject="user", attribute="wake", scope="",
                    value_kind="clock_time", unit="", operation="absolute", value=value,
                    when="2026-07-01", stated_span="I wake at 7:30")

    s1 = extract_typed_scalars_once(ep, _llm([_row("7:30")]))
    s2 = extract_typed_scalars_once(ep, _llm([_row("07:30")]))
    assert s1 and s2 and s1[0].source_key == s2[0].source_key
    out = gate_typed_scalars([s1, s2])
    assert len(out) == 1 and out[0].committed and out[0].proposal.value == "07:30"
