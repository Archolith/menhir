"""Generic synthetic tests for the event-history recall classifier + selector (Phase 4).

Covers the pure, offline, fail-closed classifier (latest / predecessor / generic-advisory /
abstain), whole-token scoping, exact-anchor resolution, and the deterministic selector that groups by
actual domain and combines unique winners by ``valid_at`` only. No benchmark IDs, answers, fixture
wording, lens/appliance examples, or rules are used.
"""

from __future__ import annotations

import pytest

from menhir.domain.event_history import TypedEventAssertion
from menhir.services.event_history_recall import (
    EventQueryRoute,
    EventQueryRouteKind,
    EventRecallGate,
    EventRecallStatus,
    ScopeStatus,
    apply_scope,
    classify_event_query,
    resolve_exact_anchor,
    select_event_recall,
)


def make(subject_uuid="subj-a", predicate="acquired", object_key="obj-x",
         domain=None, namespace=None, valid_at="2026-01-05T00:00:00Z",
         learned_at="2026-01-06T00:00:00Z", episode_uuid="ep-1", **kw) -> TypedEventAssertion:
    base = dict(
        subject_uuid=subject_uuid,
        subject_display="Alpha",
        predicate=predicate,
        object_key=object_key,
        object_display="Object X",
        domain=domain,
        namespace=namespace,
        valid_at=valid_at,
        learned_at=learned_at,
        stated_span="an exact stated span",
        episode_uuid=episode_uuid,
        span_start=10,
        span_end=30,
        claim_ordinal=0,
    )
    base.update(kw)
    return TypedEventAssertion(**base)


ROUTE = EventQueryRoute(EventQueryRouteKind.LATEST, scope_token="notebook")
ADVISORY = EventQueryRoute(EventQueryRouteKind.GENERIC_ADVISORY)


# --------------------------------------------------------------------------- latest classification

@pytest.mark.unit
def test_latest_classification_which_form() -> None:
    route = classify_event_query("which notebook did you buy most recently?")
    assert route.kind is EventQueryRouteKind.LATEST
    assert route.scope_token == "notebook"
    assert route.noun_phrase == "notebook"


@pytest.mark.unit
def test_latest_classification_type_kind_forms() -> None:
    for query in ("what type of notebook did you acquire latest?",
                  "what kind of notebook did you purchase newest?"):
        route = classify_event_query(query)
        assert route.kind is EventQueryRouteKind.LATEST
        assert route.scope_token == "notebook"
        assert route.noun_phrase == "notebook"


@pytest.mark.unit
def test_generic_advisory_populates_noun_phrase_and_none_for_nonlatest() -> None:
    advisory = classify_event_query("which item did you purchase most recently?")
    assert advisory.kind is EventQueryRouteKind.GENERIC_ADVISORY
    assert advisory.scope_token is None
    assert advisory.noun_phrase == "item"

    predecessor = classify_event_query("what did you buy before getting the blender?")
    assert predecessor.kind is EventQueryRouteKind.PREDECESSOR
    assert predecessor.noun_phrase is None

    abstain = classify_event_query("which notebook you buy most recently?")
    assert abstain.kind is EventQueryRouteKind.ABSTAIN
    assert abstain.noun_phrase is None


# --------------------------------------------------------------------------- predecessor classification

@pytest.mark.unit
def test_predecessor_anchor_strips_gerund_and_determiner() -> None:
    route = classify_event_query("what did you buy before getting the blender?")
    assert route.kind is EventQueryRouteKind.PREDECESSOR
    assert route.anchor_token == "blender"


@pytest.mark.unit
def test_verb_inside_noun_phrase_does_not_satisfy_latest_abstains() -> None:
    route = classify_event_query("which buying guide did I read most recently?")
    assert route.kind is EventQueryRouteKind.ABSTAIN


@pytest.mark.unit
def test_declarative_predecessor_statement_abstains() -> None:
    route = classify_event_query("I bought a notebook before the meeting.")
    assert route.kind is EventQueryRouteKind.ABSTAIN


@pytest.mark.unit
def test_recall_gate_is_closed_enum_and_select_uses_defined_members() -> None:
    expected = {
        EventRecallGate.ROUTE, EventRecallGate.SCOPE, EventRecallGate.ANCHOR,
        EventRecallGate.AMBIGUITY, EventRecallGate.TIME, EventRecallGate.NO_CANDIDATE,
        EventRecallGate.PASS,
    }
    assert set(EventRecallGate) == expected

    res = select_event_recall([make()], route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNIQUE
    assert res.gate is EventRecallGate.PASS


# --------------------------------------------------------------------------- abstain

@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "tell me about your day",            # unrelated
    "notebook price is high",            # scalar / no acquisition verb + form
    "which notebook did you buy latst?",  # misspelled recency
    "buy notebook latest",               # malformed (no conservative form)
    "which notebook you buy most recently?",  # missing ' did '
])
def test_unrelated_scalar_misspelled_malformed_no_did_abstain(query: str) -> None:
    route = classify_event_query(query)
    assert route.kind is EventQueryRouteKind.ABSTAIN


# --------------------------------------------------------------------------- generic advisory

@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "what kind of gadget did you buy latest?",
    "which item did you purchase most recently?",
])
def test_generic_head_is_advisory_with_no_scope_token(query: str) -> None:
    route = classify_event_query(query)
    assert route.kind is EventQueryRouteKind.GENERIC_ADVISORY
    assert route.scope_token is None


# --------------------------------------------------------------------------- whole-token scope

@pytest.mark.unit
def test_scope_applies_only_with_two_distinct_whole_token_keys() -> None:
    ev = [make(object_key="notebook-a"), make(object_key="notebook-b", span_start=40, span_end=50)]
    out = apply_scope(ev, scope_token="notebook")
    assert out.status is ScopeStatus.APPLIED
    assert {a.object_key for a in out.assertions} == {"notebook-a", "notebook-b"}


@pytest.mark.unit
def test_single_match_is_insufficient_and_preserves_input_tuple() -> None:
    ev = (make(object_key="notebook-a"), make(object_key="cup", span_start=40, span_end=50))
    out = apply_scope(ev, scope_token="notebook")
    assert out.status is ScopeStatus.INSUFFICIENT_SUPPORT
    assert out.assertions == ev


@pytest.mark.unit
def test_pen_does_not_match_pencil() -> None:
    out = apply_scope([make(object_key="pencil")], scope_token="pen")
    assert out.status is ScopeStatus.INSUFFICIENT_SUPPORT


@pytest.mark.unit
def test_plural_token_does_not_match_singular_object_key() -> None:
    out = apply_scope([make(object_key="notebook")], scope_token="notebooks")
    assert out.status is ScopeStatus.INSUFFICIENT_SUPPORT


# --------------------------------------------------------------------------- exact anchor

@pytest.mark.unit
def test_exact_anchor_zero_abstains() -> None:
    ev = [make(object_key="kettle")]
    assert resolve_exact_anchor(ev, anchor_token="blender") is None


@pytest.mark.unit
def test_exact_anchor_one_resolves() -> None:
    ev = [make(object_key="blender")]
    assert resolve_exact_anchor(ev, anchor_token="blender") is not None


@pytest.mark.unit
def test_exact_anchor_multiple_distinct_abstains() -> None:
    ev = [
        make(object_key="blender", span_start=10, span_end=20),
        make(object_key="blender", span_start=40, span_end=50, claim_ordinal=1),
    ]
    assert resolve_exact_anchor(ev, anchor_token="blender") is None


# --------------------------------------------------------------------------- namespace / subject isolation

@pytest.mark.unit
def test_select_isolates_namespace_and_subject() -> None:
    ev = [
        make(valid_at="2026-03-05T00:00:00Z", domain="kitchen"),
        make(valid_at="2026-04-05T00:00:00Z", domain="office", subject_uuid="subj-b"),
        make(valid_at="2026-04-05T00:00:00Z", domain="office", namespace="other-ns"),
        make(valid_at="2026-04-05T00:00:00Z", domain="office", predicate="sold"),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNIQUE
    assert res.selected is not None
    assert res.selected.subject_uuid == "subj-a"
    assert res.selected.namespace is None


# --------------------------------------------------------------------------- cross-domain selection

@pytest.mark.unit
def test_cross_domain_latest_chooses_greatest_valid_at() -> None:
    ev = [
        make(valid_at="2026-01-05T00:00:00Z", domain="kitchen", object_key="kettle"),
        make(valid_at="2026-02-05T00:00:00Z", domain="office", object_key="desk",
             span_start=40, span_end=50),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNIQUE
    assert res.selected.object_key == "desk"
    assert res.selected.domain == "office"


@pytest.mark.unit
def test_same_time_distinct_cross_domain_winners_are_ambiguous() -> None:
    ev = [
        make(valid_at="2026-02-05T00:00:00Z", domain="kitchen", object_key="kettle"),
        make(valid_at="2026-02-05T00:00:00Z", domain="office", object_key="desk",
             span_start=40, span_end=50),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.AMBIGUOUS
    assert res.selected is None


@pytest.mark.unit
def test_within_domain_same_time_ambiguity_fails_overall() -> None:
    ev = [
        make(valid_at="2026-02-05T00:00:00Z", domain="kitchen", object_key="kettle"),
        make(valid_at="2026-02-05T00:00:00Z", domain="kitchen", object_key="spatula",
             span_start=40, span_end=50),
        make(valid_at="2026-03-05T00:00:00Z", domain="office", object_key="desk",
             span_start=60, span_end=70),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.AMBIGUOUS
    assert res.selected is None


@pytest.mark.unit
def test_predecessor_uses_anchor_valid_at_across_domains() -> None:
    ev = [
        make(valid_at="2026-01-05T00:00:00Z", domain="kitchen", object_key="kettle"),
        make(valid_at="2026-02-05T00:00:00Z", domain="kitchen", object_key="spatula",
             span_start=40, span_end=50),
        make(valid_at="2026-03-05T00:00:00Z", domain="office", object_key="blender",
             span_start=60, span_end=70),
        make(valid_at="2026-04-05T00:00:00Z", domain="office", object_key="desk",
             span_start=80, span_end=90),
    ]
    route = classify_event_query("what did you buy before getting the blender?")
    assert route.kind is EventQueryRouteKind.PREDECESSOR
    res = select_event_recall(ev, route=route, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNIQUE
    assert res.selected.object_key == "spatula"
    assert res.selected.domain == "kitchen"


# --------------------------------------------------------------------------- authority / time

@pytest.mark.unit
def test_learned_at_never_breaks_distinct_valid_at_tie() -> None:
    ev = [
        make(valid_at="2026-02-05T00:00:00Z", domain="kitchen", object_key="kettle",
             learned_at="2026-01-01T00:00:00Z"),
        make(valid_at="2026-02-05T00:00:00Z", domain="office", object_key="desk",
             span_start=40, span_end=50, learned_at="2026-01-09T00:00:00Z"),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.AMBIGUOUS
    assert res.selected is None


@pytest.mark.unit
def test_invalid_valid_at_cannot_win() -> None:
    ev = [
        make(valid_at="not-a-time", domain="office", object_key="desk"),
        make(valid_at="2026-01-05T00:00:00Z", domain="kitchen", object_key="kettle",
             span_start=40, span_end=50),
    ]
    res = select_event_recall(ev, route=ADVISORY, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNIQUE
    assert res.selected.object_key == "kettle"


# --------------------------------------------------------------------------- fail-closed outcomes

@pytest.mark.unit
def test_abstain_route_returns_unclassified_and_no_selection() -> None:
    route = classify_event_query("which notebook you buy most recently?")
    assert route.kind is EventQueryRouteKind.ABSTAIN
    res = select_event_recall([make()], route=route, subject_uuid="subj-a")
    assert res.status is EventRecallStatus.UNCLASSIFIED
    assert res.selected is None


@pytest.mark.unit
def test_selected_populated_only_on_unique() -> None:
    single = select_event_recall(
        [make(valid_at="2026-01-05T00:00:00Z")], route=ADVISORY, subject_uuid="subj-a")
    assert single.status is EventRecallStatus.UNIQUE
    assert single.selected is not None

    tie = select_event_recall(
        [make(object_key="a"), make(object_key="b", span_start=40, span_end=50)],
        route=ADVISORY, subject_uuid="subj-a")
    assert tie.status is EventRecallStatus.AMBIGUOUS
    assert tie.selected is None

    no_scope = select_event_recall(
        [make(object_key="notebook-a")], route=ROUTE, subject_uuid="subj-a")
    assert no_scope.status is EventRecallStatus.INSUFFICIENT_SCOPE
    assert no_scope.selected is None


@pytest.mark.unit
def test_input_order_invariance_for_selection() -> None:
    base = [
        make(valid_at="2026-01-05T00:00:00Z", domain="kitchen", object_key="kettle"),
        make(valid_at="2026-02-05T00:00:00Z", domain="office", object_key="desk",
             span_start=40, span_end=50),
    ]
    fwd = select_event_recall(base, route=ADVISORY, subject_uuid="subj-a")
    rev = select_event_recall(list(reversed(base)), route=ADVISORY, subject_uuid="subj-a")
    assert fwd.status is rev.status is EventRecallStatus.UNIQUE
    assert fwd.selected.object_key == rev.selected.object_key == "desk"
