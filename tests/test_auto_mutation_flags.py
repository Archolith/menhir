"""MENHIR_AUTO_MERGE_MODE / MENHIR_AUTO_DELETE_MODE: automatic mutation is off | propose | on.

Offline. Real coordinators and a real journal over fake graph adapters; only the graph is faked.
Invariant: no automatic merge unless the merge switch is "on", no automatic delete unless the
delete switch is "on", and the two never influence each other.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import auto_mutation_policy as amp
from menhir.services.correlation_service import CorrelationService
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.lifecycle_consolidation import LifecycleConsolidationMixin
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.saga_reconcile_outcomes import REPLAYED

pytestmark = pytest.mark.unit

MODES = ("off", "propose", "on")
_BOTH = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
_GONE = {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}


def _modes(monkeypatch, merge: str | None = None, delete: str | None = None) -> None:
    for name, value in (("MENHIR_AUTO_MERGE_MODE", merge), ("MENHIR_AUTO_DELETE_MODE", delete)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# --------------------------------------------------------------------------- fakes
class _Graph:
    def __init__(self, state=None):
        self.state = dict(state or _BOTH)
        self.stamp = None
        self.merge_calls = 0
        self.capture_calls = 0

    def evaluate_merge_eligibility(self, survivor_uuid, absorbed_uuid):
        return SimpleNamespace(allowed=True, reason_code=None, diagnostics={})

    def capture_merge_snapshot(self, survivor_uuid, absorbed_uuid, *, similarity=None):
        self.capture_calls += 1
        node = lambda u: ms.encode_node(uuid=u, labels=["Entity"], properties={}, relationships=[])  # noqa: E731
        return ms.build_snapshot(
            survivor=node(survivor_uuid), absorbed=node(absorbed_uuid), similarity=similarity
        )

    def fetch_merge_state(self, survivor_uuid, absorbed_uuid):
        return {**self.state, "last_merge_op_id": self.stamp}

    def merge_entity(self, survivor_uuid, absorbed_uuid, *, similarity, operation_id=None,
                     expected_fact_uuids=None):
        self.merge_calls += 1
        self.state = dict(_GONE)
        self.stamp = operation_id
        return {"merged": 1}


class _Nodes:
    def __init__(self, expired=("n1", "n2")):
        self.present = set(expired)
        self.expired = list(expired)
        self.fetch_calls = 0
        self.delete_calls = 0

    def fetch_ttl_expired_session_uuids(self, session_id):
        self.fetch_calls += 1
        return [{"uuid": u} for u in self.expired if u in self.present]

    def capture_node_state(self, uuid):
        return {"uuid": uuid} if uuid in self.present else None

    def newly_unreferenced_evidence(self, uuids):
        return []

    def delete_entities_returning_uuids(self, uuids, **kw):
        self.delete_calls += 1
        self.present -= set(uuids)
        return list(uuids)


@pytest.fixture
def journal(tmp_path) -> GraphOperationsJournal:
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


def _rows(journal, kind=None) -> list[dict]:
    with sqlite3.connect(journal.db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM graph_operations"
        args: tuple = ()
        if kind:
            sql += " WHERE operation_kind = ?"
            args = (kind,)
        return [dict(r) for r in conn.execute(sql, args)]


def _pending(action: str) -> list[dict]:
    return PendingActionStore().fetch_pending(action, limit=100)


def _merge(coord) -> dict:
    return coord.merge(survivor_uuid="s", absorbed_uuid="a", similarity=0.97)


# =========================================================================== settings
def test_defaults_with_both_envs_unset(monkeypatch):
    _modes(monkeypatch)
    settings = MemorySettings.from_env()
    assert (settings.auto_merge_mode, settings.auto_delete_mode) == ("propose", "off")
    assert (amp.auto_merge_mode(), amp.auto_delete_mode()) == ("propose", "off")


@pytest.mark.parametrize("raw,expected", [
    ("on", "on"), ("ON", "on"), (" Propose ", "propose"), ("OFF", "off"), ("  on\t", "on"),
])
def test_mixed_case_and_whitespace_normalise(monkeypatch, raw, expected):
    _modes(monkeypatch, merge=raw, delete=raw)
    settings = MemorySettings.from_env()
    assert settings.auto_merge_mode == expected and settings.auto_delete_mode == expected


@pytest.mark.parametrize("bad", ["", " ", "yes", "true", "1", "enabled", "proposal", "onn"])
@pytest.mark.parametrize("env", ["MENHIR_AUTO_MERGE_MODE", "MENHIR_AUTO_DELETE_MODE"])
def test_invalid_and_empty_values_raise(monkeypatch, env, bad):
    _modes(monkeypatch)
    monkeypatch.setenv(env, bad)
    with pytest.raises(ValueError, match=env):
        MemorySettings.from_env()


@pytest.mark.parametrize("bad", ["", "bogus"])
def test_resolver_fails_closed_on_unreadable_setting(monkeypatch, bad):
    _modes(monkeypatch, merge="on", delete="on")
    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", bad)
    # from_env raises as a whole, so both switches fail closed
    assert amp.auto_merge_mode() == "off"
    assert amp.auto_delete_mode() == "off"


def test_resolver_fails_closed_when_settings_read_raises(monkeypatch):
    _modes(monkeypatch, merge="on", delete="on")
    with patch.object(MemorySettings, "from_env", side_effect=RuntimeError("boom")):
        assert amp.auto_merge_mode() == "off"
        assert amp.auto_delete_mode() == "off"


def test_resolver_reads_at_call_time(monkeypatch):
    _modes(monkeypatch, merge="on", delete="off")
    assert amp.auto_merge_mode() == "on"
    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", "off")
    assert amp.auto_merge_mode() == "off"


# =========================================================================== independence
@pytest.mark.parametrize("merge_mode,delete_mode", list(itertools.product(MODES, MODES)))
def test_independence_matrix(monkeypatch, journal, merge_mode, delete_mode):
    """Each outcome depends on its own switch only."""
    _modes(monkeypatch, merge=merge_mode, delete=delete_mode)
    graph = _Graph()
    merged = _merge(MergeCoordinator(graph_adapter=graph, journal=journal))
    nodes = _Nodes()
    swept = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_expired_session_nodes()

    if merge_mode == "on":
        assert merged["merged"] == 1 and graph.merge_calls == 1
    else:
        assert merged == {"merged": 0, "reason": {
            "off": amp.AUTO_MERGE_OFF, "propose": amp.AUTO_MERGE_PROPOSED}[merge_mode]}
        assert graph.merge_calls == 0
    if delete_mode == "on":
        assert sorted(swept["deleted"]) == ["n1", "n2"] and nodes.delete_calls == 1
    else:
        assert swept["deleted"] == [] and nodes.delete_calls == 0
        assert swept["reason"] == {
            "off": amp.AUTO_DELETE_OFF, "propose": amp.AUTO_DELETE_PROPOSED}[delete_mode]


# =========================================================================== merge gate
@pytest.mark.parametrize("mode", ["off", "propose"])
def test_merge_declined_writes_no_journal_and_never_snapshots(monkeypatch, journal, mode):
    _modes(monkeypatch, merge=mode, delete="on")
    graph = _Graph()
    result = _merge(MergeCoordinator(graph_adapter=graph, journal=journal))
    assert result["merged"] == 0
    assert _rows(journal) == []
    assert graph.capture_calls == 0 and graph.merge_calls == 0


def test_merge_propose_records_one_text_free_proposal(monkeypatch, journal):
    _modes(monkeypatch, merge="propose", delete="on")
    coord = MergeCoordinator(graph_adapter=_Graph(), journal=journal)
    for _ in range(3):
        _merge(coord)
    rows = _pending(amp.MERGE_PROPOSAL)
    assert len(rows) == 1
    row = rows[0]
    assert (row["node_uuid"], row["source_uuid"], row["attempts"]) == ("a", "s", 0)
    context = json.loads(row["context"])
    assert set(context) == {"reason", "trigger", "similarity"}
    assert context["reason"] == amp.AUTO_MERGE_PROPOSED


def test_merge_off_records_nothing(monkeypatch, journal):
    _modes(monkeypatch, merge="off", delete="on")
    _merge(MergeCoordinator(graph_adapter=_Graph(), journal=journal))
    assert _pending(amp.MERGE_PROPOSAL) == []


def test_flip_between_prepare_and_apply_closes_failed_and_releases_fence(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="on")
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    real_prepare = journal.prepare

    def prepare_then_flip(**kw):
        out = real_prepare(**kw)
        monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", "propose")
        return out

    monkeypatch.setattr(journal, "prepare", prepare_then_flip)
    result = _merge(coord)
    assert result["merged"] == 0 and result["reason"] == amp.AUTO_MERGE_PROPOSED
    assert graph.merge_calls == 0
    (row,) = _rows(journal)
    assert row["state"] == "FAILED"
    assert journal.list_by_state("PREPARED") == []

    # The pair fence is released: a second prepare for the pair succeeds.
    monkeypatch.setattr(journal, "prepare", real_prepare)
    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", "on")
    assert _merge(coord)["merged"] == 1
    assert sorted(r["state"] for r in _rows(journal)) == ["COMMITTED", "FAILED"]


def _crashed_prepared_row(monkeypatch, coord, journal) -> dict:
    """A merge that crashed right after PREPARE: the row is PREPARED and nothing was applied."""
    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", "on")
    with monkeypatch.context() as m:
        m.setattr(coord, "_apply", lambda request, owner_token: {"merged": 0, "crashed": True})
        _merge(coord)
    (row,) = journal.list_by_state("PREPARED")
    return row


@pytest.mark.parametrize("mode", ["off", "propose"])
def test_replay_of_unapplied_row_is_closed_without_mutation(monkeypatch, journal, mode):
    _modes(monkeypatch, merge="on", delete="on")
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    row = _crashed_prepared_row(monkeypatch, coord, journal)

    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", mode)
    outcome, _ = coord.replay_prepared_row(row, owner_token=row["owner_token"])
    assert outcome == REPLAYED
    assert graph.merge_calls == 0
    assert journal.get(row["op_id"])["state"] == "FAILED"
    assert journal.list_by_state("PREPARED") == []


@pytest.mark.parametrize("mode", MODES)
def test_replay_of_applied_row_settles_committed_in_every_mode(monkeypatch, journal, mode):
    _modes(monkeypatch, merge="on", delete="on")
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    row = _crashed_prepared_row(monkeypatch, coord, journal)
    graph.state, graph.stamp = dict(_GONE), row["op_id"]  # the crashed attempt did land

    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", mode)
    outcome, _ = coord.replay_prepared_row(row, owner_token=row["owner_token"])
    assert outcome == REPLAYED
    assert graph.merge_calls == 0
    assert journal.get(row["op_id"])["state"] == "COMMITTED"


def test_replay_of_unapplied_row_merges_when_on(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="on")
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    row = _crashed_prepared_row(monkeypatch, coord, journal)
    coord.replay_prepared_row(row, owner_token=row["owner_token"])
    assert graph.merge_calls == 1
    assert journal.get(row["op_id"])["state"] == "COMMITTED"


# =========================================================================== classify_pair
def _svc(coord, judge: str = "merged") -> CorrelationService:
    repo = MagicMock()
    repo.fetch_pair_name_embeddings.return_value = (None, None)
    svc = CorrelationService(repo, AsyncMock(), merge_coordinator=coord)
    svc._handle_merge_proposal = AsyncMock(return_value=judge)
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("off", amp.AUTO_MERGE_OFF),
                                       ("propose", amp.AUTO_MERGE_PROPOSED)])
async def test_classify_pair_returns_none_with_reason_when_merge_not_on(
    monkeypatch, journal, mode, code
):
    _modes(monkeypatch, merge=mode, delete="on")
    graph = _Graph()
    svc = _svc(MergeCoordinator(graph_adapter=graph, journal=journal))
    assert await svc.classify_pair("src", "tgt", 0.97) == ("none", {"reason": code})
    assert graph.merge_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_classify_pair_judge_no_is_still_a_conflict(monkeypatch, journal, mode):
    _modes(monkeypatch, merge=mode, delete="on")
    graph = _Graph()
    svc = _svc(MergeCoordinator(graph_adapter=graph, journal=journal), judge="conflict")
    action, details = await svc.classify_pair("src", "tgt", 0.97)
    assert action == "conflict"
    assert details == {"reason": "merge_proposal_blocked_by_veto_or_judge"}
    assert graph.merge_calls == 0


@pytest.mark.asyncio
async def test_classify_pair_merges_when_on(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="on")
    svc = _svc(MergeCoordinator(graph_adapter=_Graph(), journal=journal))
    action, details = await svc.classify_pair("src", "tgt", 0.97)
    assert action == "merged" and details["merged"] == 1


class _Host(LifecycleConsolidationMixin):
    def __init__(self, svc, hits) -> None:
        self.graph_adapter = MagicMock()
        self.graphiti_client = AsyncMock()
        self.graphiti_client.search_scored.return_value = hits
        self.llm = None
        self.settings = SimpleNamespace(conflict_cooldown_days=30)
        self._svc = svc

    def _build_correlation_service(self):
        return self._svc


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "propose"])
async def test_batch_contradiction_check_does_not_queue_a_declined_merge(
    monkeypatch, journal, mode
):
    _modes(monkeypatch, merge=mode, delete="on")
    svc = _svc(MergeCoordinator(graph_adapter=_Graph(), journal=journal))
    host = _Host(svc, [("tgt", "T", 0.97), ("tgt2", "T2", 0.97)])
    host.record_conflict_pair = AsyncMock(return_value=("g", 2))
    count = await host._check_contradictions_batch([{"uuid": "src", "name": "source"}])
    assert count == 0
    host.record_conflict_pair.assert_not_called()


@pytest.mark.asyncio
async def test_check_correlation_does_not_count_a_declined_merge(monkeypatch, journal):
    _modes(monkeypatch, merge="propose", delete="on")
    svc = _svc(MergeCoordinator(graph_adapter=_Graph(), journal=journal))
    svc._graphiti.search_scored = AsyncMock(return_value=[("tgt", "T", 0.97)])
    result = await svc.check_correlation("src", "query text")
    assert (result.merged, result.conflicts) == (0, 0)


# =========================================================================== TTL sweep
def test_ttl_sweep_off_reads_and_deletes_nothing(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="off")
    nodes = _Nodes()
    result = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_expired_session_nodes()
    assert result == {"deleted": [], "skipped": [], "reason": amp.AUTO_DELETE_OFF}
    assert nodes.fetch_calls == 0 and nodes.delete_calls == 0
    assert _rows(journal) == [] and _pending(amp.DELETE_PROPOSAL) == []


def test_ttl_sweep_propose_records_proposals_only(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="propose")
    nodes = _Nodes()
    result = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_expired_session_nodes()
    assert result["deleted"] == [] and result["skipped"] == []
    assert result["would_delete"] == ["n1", "n2"] and result["reason"] == amp.AUTO_DELETE_PROPOSED
    assert nodes.delete_calls == 0 and _rows(journal) == []
    rows = _pending(amp.DELETE_PROPOSAL)
    assert sorted(r["node_uuid"] for r in rows) == ["n1", "n2"]
    assert all(r["attempts"] == 0 for r in rows)
    assert json.loads(rows[0]["context"]) == {
        "reason": amp.AUTO_DELETE_PROPOSED, "trigger": "demote_ttl_expiry"}


def test_ttl_sweep_on_deletes_and_completes_proposals(monkeypatch, journal):
    _modes(monkeypatch, merge="on", delete="propose")
    nodes = _Nodes()
    coord = DeleteCoordinator(graph_adapter=nodes, journal=journal)
    coord.delete_expired_session_nodes()
    assert len(_pending(amp.DELETE_PROPOSAL)) == 2
    monkeypatch.setenv("MENHIR_AUTO_DELETE_MODE", "on")
    result = coord.delete_expired_session_nodes()
    assert result["deleted"] == ["n1", "n2"]
    assert _pending(amp.DELETE_PROPOSAL) == []
    assert [r["state"] for r in _rows(journal, "SESSION_TTL_DELETE")] == ["COMMITTED"]


@pytest.mark.parametrize("mode", MODES)
def test_explicit_dry_run_is_unaffected(monkeypatch, journal, mode):
    _modes(monkeypatch, merge="on", delete=mode)
    nodes = _Nodes()
    result = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_expired_session_nodes(
        dry_run=True)
    assert result["dry_run"] is True and result["would_delete"] == ["n1", "n2"]
    assert nodes.delete_calls == 0 and _rows(journal) == [] and _pending(amp.DELETE_PROPOSAL) == []


def test_delete_entity_works_with_both_switches_off(monkeypatch, journal):
    _modes(monkeypatch, merge="off", delete="off")
    nodes = _Nodes(expired=("n1",))
    result = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_entity("n1")
    assert result["deleted"] == ["n1"]


# =========================================================================== decay and orphans
def _decay_service(tmp_path, bridge: MagicMock) -> LifecycleService:
    graph = MagicMock()
    graph.sync_edge_counts.return_value = 0
    graph.fetch_decay_candidates.side_effect = lambda freshness, **kw: (
        [{"uuid": "d1", "freshness": "COMPRESSED"}] if str(freshness).upper().endswith("COMPRESSED")
        else []
    )
    graph.bridge_and_delete = bridge
    return LifecycleService(
        graph_adapter=graph, graphiti_client=MagicMock(), llm=None,
        pending_actions=PendingActionStore(db_path=tmp_path / "decay.db"),
        settings=MemorySettings(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_decay_deletion_obeys_delete_switch(monkeypatch, tmp_path, mode):
    _modes(monkeypatch, merge="on", delete=mode)
    bridge = MagicMock(return_value={"deleted": 1, "edges_bridged": 0})
    svc = _decay_service(tmp_path, bridge)
    coordinator = MagicMock()
    coordinator.delete_decayed_node.return_value = {"deleted": ["d1"], "edges_bridged": 0}
    with patch.object(LifecycleService, "should_delete", staticmethod(lambda node: True)),             patch.object(LifecycleService, "_delete_coordinator", lambda self: coordinator):
        result = await svc.apply_decay()
    if mode == "on":
        assert coordinator.delete_decayed_node.call_count == 1 and result.deleted == 1
    else:
        assert coordinator.delete_decayed_node.call_count == 0 and result.deleted == 0
    proposals = _pending(amp.DELETE_PROPOSAL)
    assert [r["node_uuid"] for r in proposals] == (["d1"] if mode == "propose" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_orphan_episode_sweep_obeys_delete_switch(monkeypatch, mode):
    _modes(monkeypatch, merge="on", delete=mode)
    graph = MagicMock()
    graph.cleanup_orphan_episodes.return_value = 3
    graph.fetch_orphan_episode_uuids.return_value = ["e1", "e2"]
    host = SimpleNamespace(graph_adapter=graph)
    count = await LifecycleConsolidationMixin._cleanup_orphan_episodes(host, "sess")
    if mode == "on":
        assert count == 3
        graph.cleanup_orphan_episodes.assert_called_once_with("sess")
    else:
        assert count == 0
        graph.cleanup_orphan_episodes.assert_not_called()
    if mode == "propose":
        assert sorted(r["node_uuid"] for r in _pending(amp.DELETE_PROPOSAL)) == ["e1", "e2"]
    else:
        assert _pending(amp.DELETE_PROPOSAL) == []


@pytest.mark.asyncio
async def test_orphan_proposal_read_failure_never_aborts_or_deletes(monkeypatch):
    _modes(monkeypatch, merge="on", delete="propose")
    graph = MagicMock()
    graph.fetch_orphan_episode_uuids.side_effect = RuntimeError("neo4j down")
    host = SimpleNamespace(graph_adapter=graph)
    assert await LifecycleConsolidationMixin._cleanup_orphan_episodes(host, None) == 0
    graph.cleanup_orphan_episodes.assert_not_called()


def test_orphan_listing_and_sweep_share_one_predicate():
    from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository

    repo = EpisodeMaintenanceRepository.__new__(EpisodeMaintenanceRepository)
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = [{"uuid": "e1"}, {"uuid": "e2"}]
    assert repo.fetch_orphan_episode_uuids("s1") == ["e1", "e2"]
    listed = repo.neo4j.execute.call_args.args[0]
    builder, _ = repo._orphan_episode_query("s1")
    assert builder.return_raw("n.uuid AS uuid").build() == listed
    assert "DELETE" not in listed.upper()


# =========================================================================== proposal store
def test_repeat_proposals_stay_one_row_with_zero_attempts_and_no_text():
    store = PendingActionStore()
    for i in range(12):
        store.record_proposals("delete_proposal", [("n1", None, json.dumps({"reason": "R", "i": i}))])
    (row,) = store.fetch_pending("delete_proposal", limit=10)
    assert row["attempts"] == 0 and json.loads(row["context"])["i"] == 11
    assert store.count("delete_proposal") == 1


def test_repeat_keeps_created_at():
    store = PendingActionStore()
    store.record_proposals("delete_proposal", [("n1", None, "{}")])
    first = store.fetch_pending("delete_proposal")[0]["created_at"]
    store.record_proposals("delete_proposal", [("n1", "s", '{"a":1}')])
    row = store.fetch_pending("delete_proposal")[0]
    assert row["created_at"] == first and row["source_uuid"] == "s"


def test_proposal_store_failure_never_mutates(monkeypatch, journal):
    _modes(monkeypatch, merge="propose", delete="propose")
    monkeypatch.setattr(PendingActionStore, "record_proposals",
                        MagicMock(side_effect=RuntimeError("disk full")))
    graph, nodes = _Graph(), _Nodes()
    assert _merge(MergeCoordinator(graph_adapter=graph, journal=journal))["merged"] == 0
    swept = DeleteCoordinator(graph_adapter=nodes, journal=journal).delete_expired_session_nodes()
    assert swept["deleted"] == [] and swept["reason"] == amp.AUTO_DELETE_PROPOSED
    assert graph.merge_calls == 0 and nodes.delete_calls == 0 and _rows(journal) == []


@pytest.mark.parametrize("erased_column", ["node", "source"])
def test_erased_uuid_is_not_recorded_and_does_not_lose_the_batch(erased_column):
    store = PendingActionStore()
    store._ensure_ready()
    from menhir.infrastructure.pending_actions import connect_telemetry_db

    with connect_telemetry_db(store.db_path) as conn:
        conn.execute("INSERT INTO erased_subject_denylist (subject_value, op_id) VALUES (?, ?)",
                     ("erased-1", "op"))
        conn.commit()
    rows = [("a", "s", '{"r":1}'), ("erased-1", "s", '{"r":1}'), ("b", "s", '{"r":1}')]
    if erased_column == "source":
        rows = [("a", "s", '{"r":1}'), ("c", "erased-1", '{"r":1}'), ("b", "s", '{"r":1}')]
    amp.record_proposals("merge_proposal", rows)
    got = sorted(r["node_uuid"] for r in store.fetch_pending("merge_proposal", limit=10))
    assert got == ["a", "b"]


def test_suite_default_survives_a_monkeypatch_undo(monkeypatch):
    monkeypatch.setenv("MENHIR_AUTO_MERGE_MODE", "off")
    monkeypatch.delenv("MENHIR_AUTO_DELETE_MODE")
    monkeypatch.undo()
    assert (amp.auto_merge_mode(), amp.auto_delete_mode()) == ("on", "on")
