"""Contracts for the internal native Falkor repository and shared runtime seam."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.infrastructure.falkor import (
    FalkorLiteRepository,
    decode_instant,
    encode_instant,
)
from menhir.infrastructure.neo4j import SagaOwnershipRevoked, revocation_scope


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-05T00:00:00Z",
        "2026-11-05T02:00:00+02:00",
        datetime(2026, 11, 4, 19, tzinfo=timezone(timedelta(hours=-5))),
        datetime(2026, 11, 5),
    ],
)
def test_instant_representation_matches_graphiti(value):
    from graphiti_core.utils.datetime_utils import convert_datetimes_to_strings

    expected = convert_datetimes_to_strings(datetime(2026, 11, 5, tzinfo=UTC))
    assert encode_instant(value) == expected
    assert decode_instant(encode_instant(value)) == datetime(2026, 11, 5, tzinfo=UTC)


def test_canonical_instant_string_order_matches_time_order():
    instants = [datetime(2026, 11, 5, tzinfo=UTC) + timedelta(microseconds=i) for i in [1_000_000, 999999, 1, 0]]
    assert sorted(map(encode_instant, instants)) == list(map(encode_instant, sorted(instants)))


def test_dst_fold_preserves_distinct_instants():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA timezone database is not installed on this host")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    assert decode_instant(encode_instant(second)) - decode_instant(encode_instant(first)) == timedelta(hours=1)
    assert encode_instant(first) < encode_instant(second)


@pytest.mark.parametrize("value", ["invalid", "2026-11-05T00:00:00.123456789Z", 42, None])
def test_instant_refuses_invalid_or_lossy_input(value):
    with pytest.raises((TypeError, ValueError)):
        encode_instant(value)


def test_dispatch_preserves_budget_ownership_and_ambiguous_failure(tmp_path):
    calls = []

    def query(text, params, *, timeout):
        calls.append((text, params, timeout))
        if text == "ambiguous":
            raise ConnectionError("lost acknowledgement")
        return SimpleNamespace(header=[(1, "value")], result_set=[[params["instant"]]])

    repo = FalkorLiteRepository(tmp_path / "graph.rdb")
    repo._db = SimpleNamespace(select_graph=lambda _name: SimpleNamespace(query=query))
    instant = datetime(2026, 11, 5, tzinfo=UTC)
    assert repo.execute("RETURN $instant AS value", {"instant": instant}, timeout_s=0.0011) == [
        {"value": encode_instant(instant)}
    ]
    assert calls[-1][2] == 2
    with revocation_scope(lambda: False), pytest.raises(SagaOwnershipRevoked):
        repo.execute("forbidden")
    assert len(calls) == 1
    with pytest.raises(ConnectionError):
        repo.execute("ambiguous", safe_to_reexecute=True)
    assert len(calls) == 2, "ambiguous mutation was retried"
    with pytest.raises(NotImplementedError):
        repo.execute_write(lambda _: pytest.fail("unsupported callback must not run"))


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_budget_refused_before_server_start(tmp_path, timeout):
    repo = FalkorLiteRepository(tmp_path / "graph.rdb")
    with pytest.raises(ValueError):
        repo.execute("RETURN 1", timeout_s=timeout)
    assert repo._db is None


def test_close_persists_once_and_cannot_reopen(tmp_path):
    calls = []
    repo = FalkorLiteRepository(tmp_path / "graph.rdb")
    repo._db = SimpleNamespace(
        client=SimpleNamespace(save=lambda: calls.append("save")), close=lambda: calls.append("close")
    )
    repo.close()
    repo.close()
    assert calls == ["save", "close"]
    with pytest.raises(RuntimeError, match="closed"):
        repo.execute("RETURN 1")


def test_failed_persistence_keeps_owner_available_for_recovery(tmp_path):
    calls = []

    def save():
        raise OSError("disk full")

    repo = FalkorLiteRepository(tmp_path / "graph.rdb")
    repo._db = SimpleNamespace(client=SimpleNamespace(save=save), close=lambda: calls.append("close"))
    with pytest.raises(OSError, match="disk full"):
        repo.close()
    assert not repo._closed and calls == []


def test_injected_backend_assembly_failure_does_not_silently_degrade(tmp_path, monkeypatch):
    from menhir.config import MemorySettings
    from menhir.core import bootstrap

    repo = FalkorLiteRepository(tmp_path / "graph.rdb")
    monkeypatch.setattr(repo, "graphiti_driver", lambda: object())

    def fail(*args, **kwargs):
        raise RuntimeError("matching Graphiti provider failed")

    monkeypatch.setattr(bootstrap.GraphitiClient, "from_settings_with_capabilities", fail)
    with pytest.raises(RuntimeError, match="matching Graphiti provider failed"):
        bootstrap.build_memory_services(MemorySettings(), graph_repository=repo)


@pytest.mark.online
@pytest.mark.asyncio
async def test_shared_bootstrap_temporal_graphiti_and_restart(tmp_path: Path, monkeypatch):
    pytest.importorskip("redislite.falkordb_client")
    from graphiti_core.nodes import EntityNode

    from menhir.config import MemorySettings
    from menhir.core.bootstrap import build_memory_services, prepare_memory_runtime

    repo = FalkorLiteRepository(tmp_path / "memory.rdb", database="menhir_test")
    built = None
    try:
        built = build_memory_services(MemorySettings(local_llm_embed_model="test-embedding"), graph_repository=repo)
        driver = repo.graphiti_driver()
        original_pid = repo._get_db().client.pid
        assert repo._get_db().client.connection_pool.connection_kwargs["retry"].get_retries() == 0
        assert driver.client.connection.connection_pool.connection_kwargs["retry"].get_retries() == 0
        assert built.neo4j is repo and built.graph_adapter.neo4j is repo
        assert built.graphiti_client.client.driver is driver
        if driver._init_task is not None:
            await driver._init_task
        repo.execute("CREATE (:Entity {uuid:'legacy-schema',source:'user',user_flagged:true})")
        assert not built.graph_adapter.phase_one_schema_ready()
        execute = repo.execute

        def interrupted_backfill(query, *args, **kwargs):
            if "SET n.type" in query:
                raise RuntimeError("injected data-backfill interruption")
            return execute(query, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(repo, "execute", interrupted_backfill)
            with pytest.raises(RuntimeError, match="schema remains absent"):
                await prepare_memory_runtime(built)
        assert not built.graph_adapter.phase_one_schema_ready(), "operational DDL hid failed data defaults"
        ready = await prepare_memory_runtime(built)
        assert ready["schema"]["status"] == "ok" and not ready["schema"]["skipped"]
        assert built.graph_adapter.phase_one_schema_ready()
        assert repo.execute(
            "MATCH (n:Entity {uuid:'legacy-schema'}) RETURN n.source AS source,n.user_flagged AS flagged"
        ) == [{"source": "user", "flagged": True}]
        assert (await prepare_memory_runtime(built))["schema"]["skipped"]
        today = datetime.now(UTC).date().isoformat()
        reminder = built.graph_adapter.create_temporal(
            content="native runtime reminder", target_date=today, namespace="tenant-a"
        )
        assert [r["uuid"] for r in built.graph_adapter.list_temporal_in_window(namespace="tenant-a")] == [
            reminder["uuid"]
        ]
        assert built.graph_adapter.list_temporal_in_window(namespace="tenant-b") == []
        assert built.graph_adapter.complete_temporal(reminder["uuid"])
        assert not built.graph_adapter.complete_temporal(reminder["uuid"])
        assert built.graph_adapter.list_temporal_in_window(namespace="tenant-a") == []
        instant = datetime(2026, 11, 5, 2, tzinfo=timezone(timedelta(hours=2)))
        node = EntityNode(uuid="shared-node", name="shared", group_id="tenant-a", created_at=instant)
        await node.save(driver)
        restored = await EntityNode.get_by_uuid(driver, "shared-node")
        assert decode_instant(restored.created_at) == decode_instant(instant)
        assert repo.execute("MATCH (n:Entity {uuid:'shared-node'}) RETURN n.created_at AS instant") == [
            {"instant": encode_instant(instant)}
        ]
        # Actual store-side comparisons use canonical instants; equivalent offsets share identity.
        assert repo.execute(
            "MATCH (n:Entity {uuid:'shared-node'}) WHERE n.created_at <= $bound RETURN n.uuid AS uuid",
            {"bound": encode_instant("2026-11-05T00:00:00Z")},
        ) == [{"uuid": "shared-node"}]
    finally:
        if built is not None:
            await built.graphiti_client.close()
        elif repo._driver is not None:
            await repo._driver.close()
        repo.close()
    reopened = FalkorLiteRepository(repo.path, database=repo.database)
    try:
        assert reopened._get_db().client.pid != original_pid, "restart reattached to the old server"
        assert reopened.execute("MATCH (n:Entity {uuid:'shared-node'}) RETURN n.name AS name") == [{"name": "shared"}]
    finally:
        reopened.close()
