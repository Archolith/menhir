"""Live proof that a same-slot value REPLACEMENT supersedes under the REAL uniqueness constraint.

This closes the seam that hid the supersession bug for a long time
(plan menhir-scalar-view-supersession-dedup-race.md): the OFFLINE scalar-View suite
(test_scalar_state_view.py) drives supersession through a fake Neo4j that has NO
`scalar_state_view_current_key_unique` constraint, so it reported supersession as working while
production silently failed; the ONLINE suite activates the constraint but never drove a plain
same-slot value change (own 20 -> own 37), the ONLY shape that trips it. Neo4j enforces property
uniqueness EAGERLY, so creating the new current View while the old node still held
`ss_view_key_current` tripped the constraint on EVERY supersession and the write deduped onto the
STALE node. These tests execute the real write against the constraint and assert the durable outcome
a fake cannot: the current View becomes the NEW value, carried by exactly ONE current-key holder.

Runs only with `--run-online` against the stood-up TEST instance (:7688); the autouse
`force_all_tests_onto_test_neo4j` fixture guarantees the target is never prod.
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
    adapter.activate_scalar_state()            # brings the uniqueness constraint online (fresh-only)
    return ViewRepository(test_neo4j_repo)


def _current_view(repo, subject_uuid):
    rows = repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "WHERE coalesce(n.view_current, true) "
        "RETURN n.ss_value AS value, n.ss_attribute AS attribute",
        {"u": subject_uuid},
    )
    return rows[0] if rows else None


def _current_key_holders(repo, subject_uuid):
    rows = repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "WHERE n.ss_view_key_current IS NOT NULL RETURN count(n) AS c",
        {"u": subject_uuid},
    )
    return int(rows[0]["c"]) if rows else 0


@pytest.mark.online
@pytest.mark.parametrize("value_kind,stale,current,current_norm", [
    ("count", 20, 37, "37"),
    ("clock_time", "09:00", "07:30", "07:30"),
])
def test_same_slot_value_replacement_supersedes(live_views, test_neo4j_repo,
                                                value_kind, stale, current, current_norm):
    # Regression: a plain same-slot value change must SUPERSEDE to a new current View, not trip the
    # eager uniqueness constraint and dedup onto the stale node. This is the exact path that was
    # broken and invisible to both the offline fake and the pre-existing online tests.
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="owned", scope="", value_kind=value_kind, unit="")

    r1 = live_views.record_scalar_state(
        subject="me", subject_uuid=subj, value=stale,
        valid_at="2026-07-01T00:00:00+00:00", **slot)
    assert r1.get("created") is True, r1

    r2 = live_views.record_scalar_state(
        subject="me", subject_uuid=subj, value=current,
        valid_at="2026-07-02T00:00:00+00:00", **slot)
    # THE regression assertion: the replacement superseded; it did NOT dedup onto the stale node.
    assert r2.get("superseded") is True, r2
    assert not r2.get("deduped"), r2

    cur = _current_view(test_neo4j_repo, subj)
    assert cur is not None and str(cur["value"]) == current_norm, cur
    # exactly ONE node bears the current-key marker for this slot (constraint satisfied, no orphan)
    assert _current_key_holders(test_neo4j_repo, subj) == 1


@pytest.mark.online
def test_three_way_replacement_keeps_single_current(live_views, test_neo4j_repo):
    # A longer chain (20 -> 37 -> 50) must leave exactly one current View at the final value with one
    # current-key holder, proving each supersession clears the prior marker before the next takes it.
    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="owned", scope="", value_kind="count", unit="")
    for i, v in enumerate((20, 37, 50)):
        res = live_views.record_scalar_state(
            subject="me", subject_uuid=subj, value=v,
            valid_at=f"2026-07-0{i + 1}T00:00:00+00:00", **slot)
        assert res.get("created") or res.get("superseded"), (v, res)
        assert not res.get("deduped"), (v, res)

    cur = _current_view(test_neo4j_repo, subj)
    assert cur is not None and str(cur["value"]) == "50", cur
    assert _current_key_holders(test_neo4j_repo, subj) == 1
