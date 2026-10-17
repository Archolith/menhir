"""4R gate: a bridge quote (one that overlaps 2+ quotes of one other sample) no longer vetoes the
agreement the other samples reached. Shapes come from the audited model-kit run (lme-gpt4_59c863d7):
11 of 20 proposals were dropped because one wide quote made a whole overlap component ambiguous."""

from __future__ import annotations

import random

import pytest

from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    _claim_groups,
    gate_typed_scalars,
)

_KW = dict(threshold=2 / 3, reconcile_attribute=True, reconcile_scope=True,
           reconcile_subject=True, align_spans=True)


def _prop(start, end, attr="model_kit_acquisition_count", ep="ep-1"):
    return TypedScalarProposal(
        subject_text="user", attribute=attr, scope="", value_kind="count", unit="",
        operation="occurrence", value=1, stated_span="x" * (end - start), episode_uuid=ep,
        span_start=start, span_end=end, claim_ordinal=1, importance="tracked")


def _committed_spans(samples, **kw):
    return sorted((d.proposal.span_start, d.proposal.span_end)
                  for d in gate_typed_scalars(samples, **_KW, **kw) if d.committed)


@pytest.mark.unit
def test_list_wide_quote_no_longer_vetoes_item_agreement():
    # Sample 0 quotes the whole list; samples 1 and 2 quote each item. Off: every item 1/3.
    items = [_prop(0, 8), _prop(12, 20), _prop(24, 30)]
    samples = [[_prop(0, 30)], list(items), list(items)]
    assert _committed_spans(samples) == []
    assert _committed_spans(samples, resolve_bridges=True) == [(0, 8), (12, 20), (24, 30)]


# Real offsets from episode 20b2517e of the audited run: two samples quote one long span covering a
# kit finished AND a kit acquired + store visit; the third splits them three ways.
_FINISHED = "model_kit_finished_count"
_STORE = "hobby_store_visit_count"
_EP_20B = [
    [_prop(132, 180, _FINISHED), _prop(150, 254)],
    [_prop(141, 180, _FINISHED), _prop(188, 254)],
    [_prop(132, 180, _FINISHED), _prop(188, 207), _prop(208, 254, _STORE)],
]
# Episode 1d7dfaed: one wide acquisition quote spans the third sample's acquisition AND show visit.
_EP_1D = [
    [_prop(184, 203), _prop(208, 260)],
    [_prop(191, 203), _prop(208, 231)],
    [_prop(195, 203), _prop(208, 231), _prop(232, 260, "model_show_visit_count")],
]


@pytest.mark.unit
def test_real_model_kit_shapes_recover_the_agreed_rows():
    assert _committed_spans(_EP_20B) == []
    # Widest bridge first: the 104-char quote goes, so the finished kit is unanimous again. The
    # acquisition stays dropped: its quotes overlap the store visit and geometry cannot place it.
    assert _committed_spans(_EP_20B, resolve_bridges=True) == [(141, 180)]
    assert _committed_spans(_EP_1D) == [(195, 203)]
    assert _committed_spans(_EP_1D, resolve_bridges=True) == [(195, 203), (208, 231)]


@pytest.mark.unit
def test_v2_attribute_split_still_runs_first_and_bridges_add_nothing_there():
    for ep in (_EP_20B, _EP_1D):
        v2 = _committed_spans(ep, counting_v2=True)
        assert _committed_spans(ep, counting_v2=True, resolve_bridges=True) == v2
    assert _committed_spans(_EP_20B, counting_v2=True) == [(141, 180), (188, 207)]


@pytest.mark.unit
def test_ambiguity_without_a_bridge_stays_ungrouped():
    # Sample 0 appears twice in one chain, but no single quote overlaps two of one sample.
    samples = [[_prop(0, 10), _prop(13, 20)], [_prop(8, 12)], [_prop(11, 14)]]
    groups = _claim_groups(samples, align_spans=True, resolve_bridges=True)
    assert all(len(g) == 1 for g in groups)
    assert _committed_spans(samples, resolve_bridges=True) == []


@pytest.mark.unit
def test_bridge_never_votes_and_drops_as_its_own_singleton():
    items = [_prop(0, 8), _prop(12, 20)]
    samples = [[_prop(0, 20)], list(items), list(items)]
    decisions = gate_typed_scalars(samples, **_KW, resolve_bridges=True)
    wide = [d for d in decisions if not d.committed]
    assert len(wide) == 1 and wide[0].agreement == pytest.approx(1 / 3)
    assert all(d.agreement == pytest.approx(2 / 3) for d in decisions if d.committed)


def _random_samples(rng):
    samples = []
    for _ in range(3):
        sample = []
        for _ in range(rng.randint(0, 4)):
            start = rng.randint(0, 60)
            sample.append(_prop(start, start + rng.randint(1, 30), ep=rng.choice(("e1", "e2"))))
        samples.append(sample)
    return samples


def _group_keys(groups):
    return {tuple(sorted((si, p.span_start, p.span_end, p.episode_uuid) for si, p in g))
            for g in groups if len(g) >= 2}


@pytest.mark.unit
def test_random_spans_keep_every_existing_group_and_never_form_an_invalid_one():
    rng = random.Random(4171)
    for _ in range(3000):
        samples = _random_samples(rng)
        before = _group_keys(_claim_groups(samples, align_spans=True))
        after_groups = _claim_groups(samples, align_spans=True, resolve_bridges=True)
        after = _group_keys(after_groups)
        # Groups that pass today are untouched; resolution only adds groups.
        assert before <= after, samples
        for g in after_groups:
            assert len({si for si, _ in g}) == len(g)              # one vote per sample
            assert len({p.episode_uuid for _, p in g}) == 1
            assert max(p.span_start for _, p in g) < min(p.span_end for _, p in g)
        # Every proposal lands in exactly one group.
        assert sum(len(g) for g in after_groups) == sum(len(s) for s in samples)


@pytest.mark.unit
def test_random_spans_commit_the_same_spans_in_any_sample_order():
    rng = random.Random(9133)
    for _ in range(1500):
        samples = _random_samples(rng)
        base = _committed_spans(samples, resolve_bridges=True)
        for order in ((1, 2, 0), (2, 0, 1), (2, 1, 0)):
            assert _committed_spans([samples[i] for i in order], resolve_bridges=True) == base


@pytest.mark.unit
def test_service_forwards_counting_match_as_resolve_bridges(monkeypatch):
    from unittest.mock import MagicMock

    from menhir.services import typed_scalar_service as tss

    seen: list[dict] = []

    def fake_gate(samples, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(tss, "gate_typed_scalars", fake_gate)
    monkeypatch.setattr(tss, "extract_typed_scalars_once", lambda *a, **kw: [])
    monkeypatch.setattr(tss, "bind_and_persist_typed_scalars", lambda *a, **kw: {"bound": 0})
    service = tss.TypedScalarPerceptionService.__new__(tss.TypedScalarPerceptionService)
    monkeypatch.setattr(type(service), "ensure_activated", lambda self: None)
    monkeypatch.setattr(type(service), "_make_self_seam", lambda self: (lambda _ns: None))
    service._adapter = MagicMock()
    service._service = MagicMock()
    service._perceiver_version = "test"
    service._embed = None
    service._embed_version = None
    service._scalar_history_enabled = False

    service.perceive_and_persist([], lambda *a, **kw: "[]", k=3)
    service.perceive_and_persist([], lambda *a, **kw: "[]", k=3, counting_match=True)
    assert [kw["resolve_bridges"] for kw in seen] == [False, True]
