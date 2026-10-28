"""#170 item 2: discovery from the default new-install route, on a real Neo4j.

A fresh install has no capture hook, so its only user input is add_memory(user_statement=...):
agent_quoted :TurnEvidence, usually with no namespace. These tests prove every lane discovers it,
across mixed namespaces, legacy spellings of the default silo, late-arriving input and multi-page
cursors, and that the counter watermark keeps a turn recorded mid-run.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.namespace import TURN_EVIDENCE_NULL_NAMESPACE_KEY
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

AT = "2026-10-19T00:00:00Z"


def _pv() -> str:
    # Unique per test: a watermark stamped by another test's version reads as reset.
    return f"pv-{uuidlib.uuid4().hex[:8]}"


def _ns() -> str:
    return f"ns-{uuidlib.uuid4().hex[:8]}"


def _record(raw, *, ns, text, source_kind="agent_quoted", occurred_at=None) -> str:
    return TurnEvidenceRepository(raw).record_turn_evidence(
        text=text, role="user", declarant="user", namespace=ns, source_kind=source_kind,
        session_id="s", occurred_at=occurred_at,
        prompt_id=f"p-{uuidlib.uuid4().hex}")["turn_id"]


def _seed_legacy(raw, *, namespace, text, recorded_at) -> str:
    """A row as pre-#170 code stored it: raw namespace (None or ''), fixed recorded_at."""
    turn_id = str(uuidlib.uuid4())
    props = {"turn_key": f"legacy-{turn_id}", "turn_id": turn_id, "role": "user",
             "declarant": "user", "text": text, "source_kind": "agent_quoted"}
    if namespace is not None:
        props["namespace"] = namespace
    raw.execute("CREATE (t:TurnEvidence) SET t = $props, t.recorded_at = datetime($at)",
                {"props": props, "at": recorded_at})
    return turn_id


def _drain(load, advance, ns, pv, *, page=50) -> list[str]:
    seen: list[str] = []
    while True:
        rows = load(ns, perceiver_version=pv, limit=page)
        if not rows:
            return seen
        seen.extend(r["uuid"] for r in rows)
        advance(ns, cursor_at=rows[-1]["cursor_at"], cursor_uuid=rows[-1]["uuid"],
                perceiver_version=pv, at=AT)


def _lanes(adapter):
    return {
        "scalar": (adapter.load_next_scalar_batch, adapter.advance_scalar_cursor),
    }


@pytest.mark.online
def test_namespace_less_agent_quoted_turn_is_stamped_and_reaches_every_lane(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    tid = _record(test_neo4j_repo, ns=None, text="I run 12 miles every week")
    stored = test_neo4j_repo.execute(
        "MATCH (t:TurnEvidence {turn_id: $t}) RETURN t.namespace AS ns", {"t": tid})
    assert stored[0]["ns"] == "default"

    dirty = {"scalar": adapter.list_scalar_dirty_namespaces}
    for name, (load, advance) in _lanes(adapter).items():
        pv = _pv()
        assert "default" in dirty[name](perceiver_version=pv, limit=10_000), name
        assert tid in _drain(load, advance, "default", pv), name
    assert "default" in adapter.list_dirty_namespaces(limit=10_000)
    assert tid in {r["uuid"] for r in adapter.load_user_episodes("default", limit=100_000)}


@pytest.mark.online
def test_legacy_spellings_join_default_without_skip_or_replay(test_neo4j_repo):
    """Upgrade: the 'default' cursor is already past legacy null/'' rows no lane ever saw."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    labels = {"scalar": "ScalarConsolidationWatermark"}
    for name, (load, advance) in _lanes(adapter).items():
        pv = _pv()
        null_row = _seed_legacy(test_neo4j_repo, namespace=None, text="I own 4 old bikes",
                                recorded_at="2000-01-01T00:00:00Z")
        empty_row = _seed_legacy(test_neo4j_repo, namespace="", text="I own 5 old bikes",
                                 recorded_at="2000-01-02T00:00:00Z")
        seen_before = _record(test_neo4j_repo, ns="default", text="I own 3 old bikes")
        # Pre-#170 state: only the 'default' watermark exists, already past seen_before.
        test_neo4j_repo.execute(
            f"MATCH (t:TurnEvidence {{turn_id: $t}}) "
            f"MERGE (w:{labels[name]} {{group_id: 'default'}}) "
            "SET w.cursor_at = t.recorded_at, w.cursor_uuid = t.turn_id, "
            "w.perceiver_version = $pv", {"t": seen_before, "pv": pv})
        seen = _drain(load, advance, "default", pv)
        assert null_row in seen and empty_row in seen     # older than the cursor, still found
        assert seen_before not in seen                     # never replayed
        assert _drain(load, advance, "default", pv) == []  # exactly once


@pytest.mark.online
def test_mixed_namespaces_keep_independent_cursors(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    for name, (load, advance) in _lanes(adapter).items():
        pv, a, b = _pv(), _ns(), _ns()
        ta = _record(test_neo4j_repo, ns=a, text="I keep 4 cats at home")
        assert _drain(load, advance, a, pv) == [ta], name
        tb = _record(test_neo4j_repo, ns=b, text="I keep 6 dogs at home")
        assert _drain(load, advance, a, pv) == [], name
        assert _drain(load, advance, b, pv) == [tb], name


@pytest.mark.online
def test_multi_page_cursor_sees_every_turn_exactly_once(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    for name, (load, advance) in _lanes(adapter).items():
        pv, ns = _pv(), _ns()
        ids = [_record(test_neo4j_repo, ns=ns, text=f"I logged {i} hours of piano")
               for i in range(23)]
        seen = _drain(load, advance, ns, pv, page=5)
        assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)), name


@pytest.mark.online
def test_late_arriving_backdated_turn_is_discovered(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    for name, (load, advance) in _lanes(adapter).items():
        pv, ns = _pv(), _ns()
        _record(test_neo4j_repo, ns=ns, text="I have 3 kayaks now")
        _drain(load, advance, ns, pv)
        late = _record(test_neo4j_repo, ns=ns, text="I had 2 kayaks in 2019",
                       occurred_at="2019-06-01T00:00:00Z")
        assert _drain(load, advance, ns, pv) == [late], name


@pytest.mark.online
def test_cursor_never_moves_backward(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    pv, ns = _pv(), _ns()
    _record(test_neo4j_repo, ns=ns, text="I have 7 plants")
    second = _record(test_neo4j_repo, ns=ns, text="I have 8 plants")
    rows = adapter.load_next_scalar_batch(ns, perceiver_version=pv, limit=10)
    adapter.advance_scalar_cursor(ns, cursor_at=rows[-1]["cursor_at"],
                                  cursor_uuid=rows[-1]["uuid"], perceiver_version=pv, at=AT)
    # A slower runner reports an older position: the cursor must not rewind.
    adapter.advance_scalar_cursor(ns, cursor_at=rows[0]["cursor_at"],
                                  cursor_uuid=rows[0]["uuid"], perceiver_version=pv, at=AT)
    assert adapter.load_next_scalar_batch(ns, perceiver_version=pv, limit=10) == []
    assert rows[-1]["uuid"] == second


@pytest.mark.online
def test_counter_lane_keeps_a_turn_recorded_mid_run(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = _ns()
    _record(test_neo4j_repo, ns=ns, text="I have 2 canoes")       # agent_quoted reaches counters
    assert ns in adapter.list_dirty_namespaces(limit=10_000)
    rows = adapter.load_user_episodes(ns)
    _record(test_neo4j_repo, ns=ns, text="I have 3 canoes")       # lands while the run works
    adapter.mark_consolidated(ns, at=rows[0]["newest_recorded_at"])
    assert ns in adapter.list_dirty_namespaces(limit=10_000)
    adapter.mark_consolidated(ns, at=adapter.load_user_episodes(ns)[0]["newest_recorded_at"])
    assert ns not in adapter.list_dirty_namespaces(limit=10_000)


@pytest.mark.online
def test_counting_lane_reads_null_spelling_rows_after_scalars(test_neo4j_repo):
    repo = TurnEvidenceRepository(test_neo4j_repo)
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    spv, cpv = _pv(), _pv()
    legacy = _seed_legacy(test_neo4j_repo, namespace=None, text="I bought 2 lamps",
                          recorded_at="2001-01-01T00:00:00Z")
    # Counting never reads ahead of scalars: nothing until the scalar lane passes the row.
    assert legacy not in {r["uuid"] for r in repo.load_next_counting_evidence_batch(
        "default", perceiver_version=cpv, scalar_perceiver_version=spv, limit=100_000)}
    _drain(adapter.load_next_scalar_batch, adapter.advance_scalar_cursor, "default", spv,
           page=1000)
    assert "default" in repo.list_counting_dirty_evidence_namespaces(
        perceiver_version=cpv, scalar_perceiver_version=spv, limit=10_000)
    rows = repo.load_next_counting_evidence_batch(
        "default", perceiver_version=cpv, scalar_perceiver_version=spv, limit=100_000)
    assert legacy in {r["uuid"] for r in rows}


@pytest.mark.online
def test_purge_default_drops_every_spelling_watermark(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    pv = _pv()
    _record(test_neo4j_repo, ns=None, text="I have 9 mugs")
    _drain(adapter.load_next_scalar_batch, adapter.advance_scalar_cursor, "default", pv,
           page=1000)
    keys = ["default", "", TURN_EVIDENCE_NULL_NAMESPACE_KEY]
    count = ("MATCH (w:ScalarConsolidationWatermark) WHERE w.group_id IN $keys "
             "RETURN count(w) AS c")
    assert test_neo4j_repo.execute(count, {"keys": keys})[0]["c"] == 3
    TurnEvidenceRepository(test_neo4j_repo).purge_namespace("default")
    assert test_neo4j_repo.execute(count, {"keys": keys})[0]["c"] == 0
    left = test_neo4j_repo.execute(
        "MATCH (t:TurnEvidence) WHERE coalesce(t.namespace, '') IN ['', 'default'] "
        "RETURN count(t) AS c")
    assert left[0]["c"] == 0
