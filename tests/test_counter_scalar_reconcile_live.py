"""Live proof for Phase 1 two-subsystem reconciliation (decision 7.A/10.A): a stated-total counter
View that DUPLICATES a typed scalar slot is retired, and the typed ScalarStateView stays authoritative.

Context (plan menhir-observation-nodes-and-view-authority-recall.md, section 1a): "I own N rare coins"
is dual-perceived into BOTH a measure-keyed counter View (`rare coins`) and an attribute-keyed typed
`absolute` (`owned`). When the two disagree (counter stuck at 20, typed head 37), recall can surface the
stale counter. `ViewRepository.retire_counters_superseded_by_scalar` retires the competing counter using
the deterministic 7.I bridge (shared episode + same subject + equal value at that episode + numeric kind),
abstaining when a counter value-matches more than one distinct typed slot.

These execute the real retire against the TEST instance (:7688); the autouse
`force_all_tests_onto_test_neo4j` fixture guarantees the target is never prod. A `FakeNeo4j` routes Cypher
by substring and cannot evaluate this multi-label join (G7), so it must be an online test.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_repository import ViewRepository


@pytest.fixture
def live_views(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()  # scalar_state uniqueness constraint online (fresh-only)
    return ViewRepository(test_neo4j_repo)


def _plant_absolute_assertion(repo, *, ns, subj, disp, attribute, value, episode,
                              value_kind="count", scope="", unit=""):
    """Create a durable :TypedAssertion (operation='absolute') the retire bridge can match. Mirrors the
    real CREATE's load-bearing props (namespace, subject_uuid/display, slot, operation, value,
    episode_uuid, binding_pending=false) without driving the LLM perception pipeline."""
    repo.execute(
        """
        CREATE (a:TypedAssertion {
            assertion_key: $ak, namespace: $ns, operation: 'absolute',
            subject_uuid: $subj, subject_display: $disp,
            attribute: $attr, scope: $scope, value_kind: $vk, unit: $unit,
            value: $value, episode_uuid: $epi, binding_pending: false
        })
        """,
        {"ak": f"ak-{uuidlib.uuid4().hex}", "ns": ns, "subj": subj, "disp": disp, "attr": attribute,
         "scope": scope, "vk": value_kind, "unit": unit, "value": str(value), "epi": episode},
    )


def _counter_current(repo, *, ns, subject, counter):
    rows = repo.execute(
        "MATCH (n:Entity {view_kind:'counter', group_id:$ns, view_subject:$s, qs_counter:$c}) "
        "RETURN coalesce(n.view_current, true) AS current, coalesce(n.retired, false) AS retired, "
        "n.view_value AS value",
        {"ns": ns, "s": subject, "c": counter},
    )
    return rows[0] if rows else None


def _scalar_current_value(repo, *, subject_uuid):
    rows = repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "WHERE coalesce(n.view_current, true) RETURN n.ss_value AS value",
        {"u": subject_uuid},
    )
    return str(rows[0]["value"]) if rows else None


@pytest.mark.online
def test_stale_stated_counter_retired_typed_view_survives(live_views, test_neo4j_repo):
    # THE bug: counter `rare coins`=20 co-extracted from E1 with the typed absolute owned=20, while the
    # typed head is now 37 (E2). The counter duplicates the typed slot and must be retired; the typed
    # ScalarStateView(37) must remain the single current authority.
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    e1, e2 = f"ep-{uuidlib.uuid4().hex[:8]}", f"ep-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="owned", scope="", value_kind="count", unit="")

    # typed log: absolute 20 @E1 (later superseded), absolute 37 @E2 (current head)
    _plant_absolute_assertion(test_neo4j_repo, ns=ns, subj=subj, disp="me",
                              attribute="owned", value=20, episode=e1)
    _plant_absolute_assertion(test_neo4j_repo, ns=ns, subj=subj, disp="me",
                              attribute="owned", value=37, episode=e2)
    # the authoritative current typed View at the head value 37
    live_views.record_scalar_state(subject="me", subject_uuid=subj, value=37,
                                   valid_at="2026-07-02T00:00:00+00:00", namespace=ns, **slot)
    # the competing stale stated counter (value 20, sharing E1 with the typed absolute)
    live_views.record_counter(subject="me", counter="rare coins", value=20, namespace=ns,
                              episode_uuids=[e1], valid_at="2026-07-01T00:00:00+00:00")

    retired = live_views.retire_counters_superseded_by_scalar(namespace=ns)

    assert retired == 1
    c = _counter_current(test_neo4j_repo, ns=ns, subject="me", counter="rare coins")
    assert c is not None and c["current"] is False and c["retired"] is True, c
    # typed authority is untouched and still current at the head value
    assert _scalar_current_value(test_neo4j_repo, subject_uuid=subj) == "37"
    # idempotent: a second pass finds no current counter to retire
    assert live_views.retire_counters_superseded_by_scalar(namespace=ns) == 0


@pytest.mark.online
def test_aggregate_counter_not_retired_when_no_value_match(live_views, test_neo4j_repo):
    # An aggregate counter (e.g. a summed total) that shares an episode with a typed slot but whose value
    # does NOT equal a typed absolute at that episode is a DIFFERENT fact and must survive.
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    e1 = f"ep-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="owned", scope="", value_kind="count", unit="")

    _plant_absolute_assertion(test_neo4j_repo, ns=ns, subj=subj, disp="me",
                              attribute="owned", value=37, episode=e1)
    live_views.record_scalar_state(subject="me", subject_uuid=subj, value=37,
                                   valid_at="2026-07-02T00:00:00+00:00", namespace=ns, **slot)
    # counter value 55 (a summed spend, say) shares E1 but matches no typed absolute value there
    live_views.record_counter(subject="me", counter="biking spend", value=55, namespace=ns,
                              episode_uuids=[e1], valid_at="2026-07-01T00:00:00+00:00")

    assert live_views.retire_counters_superseded_by_scalar(namespace=ns) == 0
    c = _counter_current(test_neo4j_repo, ns=ns, subject="me", counter="biking spend")
    assert c is not None and c["current"] is True and c["retired"] is False, c


@pytest.mark.online
def test_abstain_on_ambiguous_multi_slot_value_match(live_views, test_neo4j_repo):
    # "I have 20 coins and 20 cards": a counter value 20 sharing an episode with TWO distinct typed slots
    # both at value 20 is ambiguous -> the retire abstains (safety = keep, the authority gate decides).
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    e1 = f"ep-{uuidlib.uuid4().hex[:8]}"

    for attr in ("coins_owned", "cards_owned"):
        _plant_absolute_assertion(test_neo4j_repo, ns=ns, subj=subj, disp="me",
                                  attribute=attr, value=20, episode=e1)
        live_views.record_scalar_state(subject="me", subject_uuid=subj, value=20,
                                       valid_at="2026-07-02T00:00:00+00:00", namespace=ns,
                                       attribute=attr, scope="", value_kind="count", unit="")
    live_views.record_counter(subject="me", counter="rare coins", value=20, namespace=ns,
                              episode_uuids=[e1], valid_at="2026-07-01T00:00:00+00:00")

    assert live_views.retire_counters_superseded_by_scalar(namespace=ns) == 0
    c = _counter_current(test_neo4j_repo, ns=ns, subject="me", counter="rare coins")
    assert c is not None and c["current"] is True and c["retired"] is False, c


@pytest.mark.online
def test_other_namespace_counter_untouched(live_views, test_neo4j_repo):
    # Namespace isolation: a retire scoped to ns A must never touch a counter in ns B.
    ns_a = f"ns-{uuidlib.uuid4().hex[:8]}"
    ns_b = f"ns-{uuidlib.uuid4().hex[:8]}"
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    e1 = f"ep-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="owned", scope="", value_kind="count", unit="")

    _plant_absolute_assertion(test_neo4j_repo, ns=ns_a, subj=subj, disp="me",
                              attribute="owned", value=20, episode=e1)
    live_views.record_scalar_state(subject="me", subject_uuid=subj, value=37,
                                   valid_at="2026-07-02T00:00:00+00:00", namespace=ns_a, **slot)
    # a matching-looking counter but in ns B (no typed slot there)
    live_views.record_counter(subject="me", counter="rare coins", value=20, namespace=ns_b,
                              episode_uuids=[e1], valid_at="2026-07-01T00:00:00+00:00")

    assert live_views.retire_counters_superseded_by_scalar(namespace=ns_a) == 0
    c = _counter_current(test_neo4j_repo, ns=ns_b, subject="me", counter="rare coins")
    assert c is not None and c["current"] is True and c["retired"] is False, c
