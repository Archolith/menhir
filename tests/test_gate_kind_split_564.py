"""#564: one sample reading a span as two claims of different kinds must not void the others' vote.

Sample 1 read "pre-approved for $350,000" as both a status and an amount; samples 0 and 1 agreed on
the amount. The aligned grouper used to leave the whole component ungrouped because sample 1
appeared twice, so 2/3 agreement became three 1/3 vetoes. It now splits such a component by
(value_kind, unit) first. A component that is still ambiguous within one kind stays ungrouped.
"""

from __future__ import annotations

from menhir.services.typed_scalar_perception import TypedScalarProposal, gate_typed_scalars

EPISODE = "11111111-1111-1111-1111-111111111111"
QUOTE = "pre-approved for $350,000"
SPAN = (61, 61 + len(QUOTE))


def proposal(
    *, attribute="pre_approval_amount", value=350000, kind="money", unit="usd",
    span=SPAN, quote=QUOTE,
) -> TypedScalarProposal:
    return TypedScalarProposal(
        subject_text="user", attribute=attribute, scope="", value_kind=kind, unit=unit,
        operation="absolute", value=value, stated_span=quote, episode_uuid=EPISODE,
        span_start=span[0], span_end=span[1],
    )


def status() -> TypedScalarProposal:
    return proposal(attribute="mortgage_preapproval_status", value="pre-approved", kind="state", unit="")


def gate(samples, **kwargs):
    kwargs.setdefault("threshold", 2 / 3)
    return gate_typed_scalars(samples, reconcile_attribute=True, align_spans=True, **kwargs)


def committed(decisions):
    return [d for d in decisions if d.committed]


def test_564_amount_commits_when_one_sample_also_read_a_status():
    samples = [
        [proposal(attribute="pre_approval_amount")],
        [status(), proposal(attribute="preapproval_amount")],
        [],
    ]
    out = committed(gate(samples))
    assert len(out) == 1
    assert out[0].proposal.value == 350000
    assert out[0].proposal.value_kind == "money"
    assert out[0].proposal.attribute == "pre_approval_amount"  # longest name wins the 1-1 tie
    assert out[0].agreement >= 2 / 3


def test_564_status_read_by_one_sample_stays_uncommitted():
    samples = [
        [proposal()],
        [status(), proposal()],
        [],
    ]
    out = committed(gate(samples))
    assert all(d.proposal.value_kind == "money" for d in out)


def test_564_result_is_independent_of_sample_order():
    base = [[proposal(attribute="pre_approval_amount")], [status(), proposal(attribute="preapproval_amount")], []]
    for order in [(0, 1, 2), (2, 1, 0), (1, 0, 2)]:
        out = committed(gate([base[i] for i in order]))
        assert [(d.proposal.attribute, d.proposal.value) for d in out] == [("pre_approval_amount", 350000)]


def test_same_kind_ambiguity_stays_ungrouped():
    """One sample reading two different amounts on the span is still ambiguous: no group, no commit."""
    samples = [
        [proposal(value=350000)],
        [proposal(value=350000), proposal(attribute="down_payment", value=70000)],
        [],
    ]
    assert committed(gate(samples)) == []


def test_unambiguous_component_unchanged():
    samples = [[proposal()], [proposal(attribute="preapproval_amount")], [proposal()]]
    out = committed(gate(samples, threshold=1.0))
    assert len(out) == 1
    assert out[0].agreement == 1.0


def test_counting_lane_keeps_attribute_split():
    """counting_v2 splits ambiguous components by attribute (F12); the kind split must not replace it."""
    samples = [
        [proposal(attribute="pre_approval_amount")],
        [status(), proposal(attribute="pre_approval_amount")],
        [proposal(attribute="pre_approval_amount")],
    ]
    out = committed(gate(samples, threshold=1.0, counting_v2=True))
    assert [(d.proposal.attribute, d.proposal.value) for d in out] == [("pre_approval_amount", 350000)]
