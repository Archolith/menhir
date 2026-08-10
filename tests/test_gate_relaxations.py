"""The two opt-in relaxations in `gate_typed_scalars`: attribute reconciliation and span alignment.

Both exist because the k-sample gate was measured losing ~77% of facts the samples ALREADY AGREED
ON -- the disagreement is about the key (what to call the attribute, exactly which characters were
quoted), never about the value. These tests pin the behaviour that makes that recovery safe: the
defaults must not move, a merge must never invent agreement that was not there, and the chosen
attribute must not depend on the order the samples happened to arrive in.
"""

from __future__ import annotations

import pytest

from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    gate_typed_scalars,
)

EPISODE = "11111111-1111-1111-1111-111111111111"


def proposal(
    *, attribute="team_size", value=5, span=(0, 10), episode=EPISODE,
    subject="user", unit="", when=None, scope="", operation="absolute",
) -> TypedScalarProposal:
    return TypedScalarProposal(
        subject_text=subject, attribute=attribute, scope=scope, value_kind="count",
        unit=unit, operation=operation, value=value, stated_span="I lead 5",
        episode_uuid=episode, span_start=span[0], span_end=span[1], when=when,
    )


def committed(decisions):
    return [d for d in decisions if d.committed]


# --------------------------------------------------------------------- defaults do not move
def test_defaults_still_veto_attribute_disagreement():
    """The relaxations are opt-in. With both off, three samples that agree on the value but not the
    name must still abstain -- otherwise the flags are not flags and the rollout is not staged."""
    samples = [
        [proposal(attribute="team_size")],
        [proposal(attribute="engineer_count")],
        [proposal(attribute="count")],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0)) == []


def test_defaults_still_veto_span_drift():
    samples = [
        [proposal(span=(0, 10))],
        [proposal(span=(0, 11))],
        [proposal(span=(0, 12))],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0)) == []


# --------------------------------------------------------- attribute reconciliation
def test_reconcile_commits_when_only_the_name_differs():
    samples = [
        [proposal(attribute="team_size")],
        [proposal(attribute="team_size")],
        [proposal(attribute="count")],
    ]
    out = committed(gate_typed_scalars(samples, threshold=1.0, reconcile_attribute=True))
    assert len(out) == 1
    assert out[0].proposal.value == 5
    assert out[0].proposal.attribute == "team_size", "modal name must win"


def test_reconcile_does_not_paper_over_a_real_value_disagreement():
    """Reconciliation drops the NAME from the vote and nothing else. Samples reading different
    values are still a genuine scatter and must abstain."""
    samples = [
        [proposal(attribute="team_size", value=5)],
        [proposal(attribute="count", value=4)],
        [proposal(attribute="headcount", value=9)],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, reconcile_attribute=True)) == []


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_reconcile_tiebreak_is_independent_of_sample_order(order):
    """A 1-1-1 split has no modal name. The tie-break must be a function of the candidate SET, or the
    ScalarStateView slot a fact lands in would depend on which sample the API returned first."""
    # Deliberately chosen so LONGEST and LEXICOGRAPHIC disagree: lexicographic alone would pick
    # "count", which is exactly the generic slot the tie-break exists to avoid.
    names = ["count", "postcard_count", "zebra_count"]
    samples = [[proposal(attribute=names[i], value=25)] for i in order]
    out = committed(gate_typed_scalars(samples, threshold=1.0, reconcile_attribute=True))
    assert len(out) == 1
    assert out[0].proposal.attribute == "postcard_count", (
        "longest-then-lexicographic: the generic 'count' must never win a tie")


def test_reconcile_keeps_the_slot_internally_coherent():
    """The committed proposal must be one that ACTUALLY carries the winning name -- not the
    first-seen proposal wearing someone else's attribute, which would desync `slot_key`."""
    samples = [
        [proposal(attribute="count", unit="")],
        [proposal(attribute="team_size", unit="")],
        [proposal(attribute="team_size", unit="")],
    ]
    out = committed(gate_typed_scalars(samples, threshold=1.0, reconcile_attribute=True))
    assert out[0].proposal.attribute == "team_size"
    assert out[0].proposal.slot_key[0] == "team_size"


# ------------------------------------------------------------------- span alignment
def test_align_merges_overlapping_spans_into_one_claim():
    samples = [
        [proposal(span=(0, 20))],
        [proposal(span=(2, 22))],
        [proposal(span=(4, 18))],
    ]
    out = committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True))
    assert len(out) == 1
    assert out[0].source_key == out[0].proposal.source_key


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_align_uses_order_independent_common_grounding(order):
    """Quote-boundary variance must converge on one real substring and one durable source key."""
    content = "I have completed 30 lessons so far"
    quotes = [
        "completed 30 lessons",
        "30 lessons so far",
        "I have completed 30 lessons so far",
    ]

    def grounded(quote):
        start = content.index(quote)
        return TypedScalarProposal(
            subject_text="user",
            attribute="completed_lessons",
            scope="course",
            value_kind="count",
            unit="",
            operation="absolute",
            value=30,
            stated_span=quote,
            episode_uuid=EPISODE,
            span_start=start,
            span_end=start + len(quote),
        )

    samples = [[grounded(quotes[index])] for index in order]
    out = committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True))
    assert len(out) == 1
    proposal_out = out[0].proposal
    assert proposal_out.stated_span == "30 lessons"
    assert content[proposal_out.span_start:proposal_out.span_end] == proposal_out.stated_span
    assert out[0].source_key == proposal_out.source_key


def test_align_does_not_turn_semantic_disagreement_into_agreement():
    """A shared quote boundary cannot make a clock time and an elapsed duration agree."""
    clock = TypedScalarProposal(
        subject_text="user", attribute="meeting_time", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="18:00", stated_span="meet at 18:00",
        episode_uuid=EPISODE, span_start=0, span_end=13,
    )
    duration = TypedScalarProposal(
        subject_text="user", attribute="training_duration", scope="", value_kind="duration",
        unit="minutes", operation="absolute", value=18, stated_span="at 18:00 for training",
        episode_uuid=EPISODE, span_start=5, span_end=26,
    )
    out = committed(gate_typed_scalars(
        [[clock], [duration], []], threshold=2 / 3, align_spans=True,
        reconcile_attribute=True, reconcile_scope=True,
    ))
    assert out == []


def test_align_requires_a_span_common_to_every_member():
    """Chained pairwise overlap would drag A and C together through a long B that touches both. Only
    a span shared by ALL members is one claim."""
    samples = [
        [proposal(span=(0, 10))],
        [proposal(span=(5, 40))],
        [proposal(span=(30, 50))],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True)) == []


def test_align_never_merges_across_episodes():
    other = "22222222-2222-2222-2222-222222222222"
    samples = [
        [proposal(span=(0, 10), episode=EPISODE)],
        [proposal(span=(0, 10), episode=other)],
        [proposal(span=(0, 10), episode=EPISODE)],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True)) == []


def test_align_abstains_when_one_sample_offers_two_readings():
    """Two overlapping proposals from the SAME sample make the grouping ambiguous -- there is no
    principled way to say which one the other samples were agreeing with. Worst case must be the
    status quo, never a wrong merge."""
    samples = [
        [proposal(span=(0, 20), value=5), proposal(span=(1, 19), value=4)],
        [proposal(span=(2, 18), value=5)],
        [proposal(span=(3, 17), value=5)],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True)) == []


def test_align_leaves_unrelated_claims_separate():
    samples = [
        [proposal(span=(0, 10), value=5), proposal(span=(50, 60), value=9)],
        [proposal(span=(1, 11), value=5), proposal(span=(51, 61), value=9)],
        [proposal(span=(2, 12), value=5), proposal(span=(52, 62), value=9)],
    ]
    out = committed(gate_typed_scalars(samples, threshold=1.0, align_spans=True))
    assert sorted(d.proposal.value for d in out) == [5, 9]


# ------------------------------------------------- the setting reaches the gate
# The identity reconciliation switches are wired end to end. Span alignment is an internal safety
# invariant in the perception service rather than an operator setting: it now commits the
# deterministic common source substring, so quote-boundary drift cannot choose a sample-dependent
# durable source_key.
PLUMBING = [
    ("src/menhir/core/runtime.py", "scalar_reconcile_attribute=getattr("),
    ("src/menhir/api/routes_handlers.py", "scalar_reconcile_attribute=getattr("),
    ("src/menhir/services/maintenance_scheduler.py",
     "scalar_reconcile_attribute=self.scalar_reconcile_attribute"),
    ("src/menhir/services/scheduler_tasks.py",
     "reconcile_attribute=scalar_reconcile_attribute"),
    # The scalar pass sits behind ScalarConsolidationConfig since the wave-4 split.
    ("src/menhir/services/scalar_consolidation.py",
     "reconcile_attribute=config.reconcile_attribute"),
    ("src/menhir/services/typed_scalar_service.py",
     "reconcile_attribute=reconcile_attribute"),
]


@pytest.mark.parametrize("relative_path,forwarding", PLUMBING)
def test_every_hop_forwards_the_flag(relative_path, forwarding):
    import pathlib
    source = (pathlib.Path(__file__).parent.parent / relative_path).read_text(encoding="utf-8")
    assert forwarding in source, f"{relative_path} accepts the flag but never forwards it"


def test_align_spans_is_not_an_operator_setting():
    """Canonical source grounding is always-on internally, not a benchmark-tunable switch."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for relative_path in (
        "src/menhir/core/runtime.py", "src/menhir/api/routes_handlers.py",
        "src/menhir/services/maintenance_scheduler.py", "src/menhir/services/scheduler_tasks.py",
    ):
        assert "align_spans" not in (root / relative_path).read_text(encoding="utf-8"), (
            f"{relative_path} exposes align_spans; canonical grounding must not be run-tunable")


def test_service_forwards_reconcile_attribute_to_the_gate(monkeypatch):
    """The hop that actually changes behaviour, asserted on the real call rather than the source."""
    # Patch where perceive_and_persist RESOLVES the gate, not the compat re-export surface.
    from menhir.services import typed_scalar_service as tsp

    seen: dict[str, object] = {}

    def fake_gate(samples, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(tsp, "gate_typed_scalars", fake_gate)
    monkeypatch.setattr(tsp, "extract_typed_scalars_once", lambda *a, **kw: [])
    monkeypatch.setattr(tsp, "bind_and_persist_typed_scalars", lambda *a, **kw: {"bound": 0})

    service = tsp.TypedScalarPerceptionService.__new__(tsp.TypedScalarPerceptionService)
    monkeypatch.setattr(type(service), "ensure_activated", lambda self: None)
    monkeypatch.setattr(type(service), "_make_self_seam", lambda self: (lambda _ns: None))
    from unittest.mock import MagicMock
    service._adapter = MagicMock()
    service._service = MagicMock()
    service._perceiver_version = "test"
    service._embed = None
    service._embed_version = None
    service._scalar_history_enabled = False

    service.perceive_and_persist([], lambda *a, **kw: "[]", k=3, reconcile_attribute=True)
    assert seen.get("reconcile_attribute") is True
    assert seen.get("align_spans") is True


# ------------------------------------------------- scope / subject identity reconciliation
# Residual-loss dump (scripts/_dump_residual_losses.py) over the cells that STILL failed with
# reconcile_attribute + align_spans at threshold 2/3: the same span comes back decomposed three
# ways, e.g. attr='watched' scope='mcu_films' vs attr='mcus_watched_count' scope=''. Subject,
# value, unit, operation and `when` were identical. Fold-scored recovery: 58 -> 62 (+scope) ->
# 64 (+subject) of 100, with stale-as-current staying at 0.
def test_reconcile_scope_commits_when_only_scope_differs():
    samples = [
        [proposal(scope="mcu_films")],
        [proposal(scope="last_3_months")],
        [proposal(scope="")],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0)) == []
    out = committed(gate_typed_scalars(samples, threshold=1.0, reconcile_scope=True))
    assert len(out) == 1
    assert out[0].proposal.value == 5


def test_joint_reconciliation_never_synthesizes_an_unproposed_pair():
    """The whole reason attribute and scope are reconciled as a TUPLE. Picking the modal attribute
    and the modal scope independently would here choose `watched` + `mcu_films` -- a slot no sample
    ever proposed. The committed proposal must be one that actually exists."""
    samples = [
        [proposal(attribute="watched", scope="last_3_months")],
        [proposal(attribute="watched", scope="last_3_months")],
        [proposal(attribute="mcus_watched_count", scope="mcu_films")],
    ]
    out = committed(gate_typed_scalars(
        samples, threshold=1.0, reconcile_attribute=True, reconcile_scope=True))
    assert len(out) == 1
    assert (out[0].proposal.attribute, out[0].proposal.scope) in {
        ("watched", "last_3_months"), ("mcus_watched_count", "mcu_films")}
    assert out[0].proposal.attribute != "watched" or out[0].proposal.scope != "mcu_films"


def test_reconcile_subject_commits_when_only_the_possessive_differs():
    """'my pre-approval' / 'my loan' / 'user' for one quoted $400,000 -- three spellings of one
    subject, observed verbatim on the panel."""
    samples = [
        [proposal(subject="my pre-approval", value=400000)],
        [proposal(subject="user", value=400000)],
        [proposal(subject="my loan", value=400000)],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0)) == []
    out = committed(gate_typed_scalars(samples, threshold=1.0, reconcile_subject=True))
    assert len(out) == 1
    assert out[0].proposal.value == 400000


def test_reconcile_scope_does_not_paper_over_a_value_disagreement():
    samples = [
        [proposal(scope="current", value=5)],
        [proposal(scope="", value=4)],
        [proposal(scope="previous", value=9)],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, reconcile_scope=True)) == []


def test_reconciliation_never_drops_operation():
    """`operation` stays in the vote under every relaxation. 'I added 25' (delta) and 'I have 25'
    (absolute) fold into different Views, so a split here must abstain no matter what else is
    reconciled -- this is the one field where a majority vote can commit a confidently wrong View."""
    samples = [
        [proposal(operation="delta", value=25, attribute="count", scope="postcards")],
        [proposal(operation="absolute", value=25, attribute="postcard_count", scope="")],
        [proposal(operation="expire", value=25, attribute="collection_size", scope="x")],
    ]
    out = committed(gate_typed_scalars(
        samples, threshold=1.0, reconcile_attribute=True,
        reconcile_scope=True, reconcile_subject=True))
    assert out == []


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_joint_tiebreak_is_independent_of_sample_order(order):
    pairs = [("count", ""), ("postcard_count", "shelf"), ("zebra_count", "")]
    samples = [[proposal(attribute=a, scope=s, value=25)] for a, s in
               (pairs[i] for i in order)]
    out = committed(gate_typed_scalars(
        samples, threshold=1.0, reconcile_attribute=True, reconcile_scope=True))
    assert len(out) == 1
    assert (out[0].proposal.attribute, out[0].proposal.scope) == ("postcard_count", "shelf"), (
        "longest-combination-then-lexicographic, a pure function of the candidate set")


# ------------------------------------------------------------- self-subject canonicalization
def test_canonical_self_folds_first_person_to_the_bound_display():
    """`_is_self_reference` is what the BINDER uses; without canonical_self the vote key disagrees
    with it, so two samples that both correctly identify the self abstain over spelling.

    Measured worth on the LME panel: ZERO cells. The extraction prompt already emits 'user' almost
    without exception, so the inconsistency is latent there. Kept because it is a real contradiction
    between the vote key and binding, and it costs nothing -- not because it bought recall."""
    samples = [
        [proposal(subject="I")],
        [proposal(subject="user")],
        [proposal(subject="me")],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0)) == []
    out = committed(gate_typed_scalars(samples, threshold=1.0, canonical_self=True))
    assert len(out) == 1


def test_canonical_self_does_not_merge_distinct_third_parties():
    samples = [
        [proposal(subject="my wife")],
        [proposal(subject="user")],
        [proposal(subject="my brother")],
    ]
    assert committed(gate_typed_scalars(samples, threshold=1.0, canonical_self=True)) == []


def test_identity_switches_are_wired_through_every_layer():
    """scope/subject reconciliation and canonical_self are now settings-reachable (they replaced the
    guard that pinned them unwired). A switch that stops at any one layer is silently inert -- the
    settings flip, nothing changes, and the measurement is unreproducible -- so pin the whole chain."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for relative_path in (
        "src/menhir/core/runtime.py", "src/menhir/api/routes_handlers.py",
        "src/menhir/services/maintenance_scheduler.py", "src/menhir/services/scheduler_tasks.py",
        "src/menhir/services/scalar_consolidation.py", "src/menhir/services/typed_scalar_service.py",
        "src/menhir/config/settings_model.py",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        for switch in ("reconcile_scope", "reconcile_subject", "canonical_self"):
            assert switch in source, f"{relative_path} drops {switch}; the switch is inert"


def test_identity_switches_default_off():
    """Wiring is not activation. Every new switch must stay off unless explicitly enabled."""
    from menhir.config.settings_model import MemorySettings
    for field in ("personal_memory_scalar_reconcile_scope",
                  "personal_memory_scalar_reconcile_subject",
                  "personal_memory_scalar_canonical_self"):
        assert getattr(MemorySettings, field) is False, f"{field} must default off"


def test_align_spans_remains_internal_not_settings_reachable():
    """Span canonicalization must not become a per-run benchmark knob."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for relative_path in (
        "src/menhir/core/runtime.py", "src/menhir/api/routes_handlers.py",
        "src/menhir/services/maintenance_scheduler.py", "src/menhir/services/scheduler_tasks.py",
        "src/menhir/services/scalar_consolidation.py", "src/menhir/config/settings_model.py",
    ):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert "align_spans" not in source, f"{relative_path} exposes align_spans as a setting"


def test_both_relaxations_compose():
    samples = [
        [proposal(attribute="team_size", span=(0, 20))],
        [proposal(attribute="count", span=(2, 22))],
        [proposal(attribute="team_size", span=(4, 18))],
    ]
    out = committed(gate_typed_scalars(
        samples, threshold=1.0, reconcile_attribute=True, align_spans=True))
    assert len(out) == 1
    assert out[0].proposal.attribute == "team_size"
