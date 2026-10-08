"""P2 persist step: the anchored-time contract is written only to this call's new edges.

The graph-backed counterexamples (duplicate, replay, other tenant, other episode, expired edge)
run against Neo4j in tests/test_anchored_time_persist_live.py.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.infrastructure import graphiti_extraction_policy as extraction
from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult
from menhir.infrastructure.anchored_time_persist import (
    CONTRACT_PROPERTIES,
    PERSIST_CYPHER,
    contract_rows,
    persist_anchored_time,
)
from menhir.infrastructure.graphiti_client import GraphitiClient

pytestmark = pytest.mark.unit

NOW = datetime(2023, 5, 14, 15, 0, tzinfo=timezone.utc)


def result(uuid: str, **overrides) -> AnchoredTimeResult:
    fields = dict(
        edge_uuid=uuid, fact_index=0, expression="a month ago", basis="speech_relative",
        kind="point_event", granularity="month", window_start=date(2023, 4, 1),
        window_end=date(2023, 4, 30), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=True, reason="written", graphiti_valid_at=NOW,
        new_valid_at=datetime(2023, 4, 15, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return AnchoredTimeResult(**fields)


def report(*results: AnchoredTimeResult, status: str = "ok") -> AnchoredTimeReport:
    return AnchoredTimeReport(status=status, model="gpt-x", results=tuple(results))


class FakeDriver:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error = error

    async def execute_query(self, query, params=None, **kwargs):
        self.calls.append((query, params))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(records=[{"written": len(params["rows"])}])


# --- rows -------------------------------------------------------------------------------------


def test_rows_carry_every_contract_field_for_listed_edges_only() -> None:
    rep = report(result("new"), result("dup", fact_index=1))
    rows = contract_rows(rep, ["new"], date(2023, 5, 14))
    assert [r["uuid"] for r in rows] == ["new"]
    row = rows[0]
    assert set(row) == {"uuid", *CONTRACT_PROPERTIES}
    assert row["time_basis"] == "speech_relative"
    assert row["time_window_start"] == "2023-04-01" and row["time_window_end"] == "2023-04-30"
    assert row["time_outcome"] == "written"
    assert row["time_speech_date"] == "2023-05-14"
    assert row["time_contract"].startswith("at1|") and row["time_contract"].endswith("|gpt-x")


def test_undated_and_plan_results_are_persisted_too() -> None:
    rep = report(
        result("none", basis="none", expression=None, granularity=None, window_start=None,
               window_end=None, written=False, reason="undated", new_valid_at=None),
        result("plan", kind="plan", planned_window=(date(2023, 6, 1), date(2023, 6, 30)),
               written=False, reason="plan", new_valid_at=None),
    )
    rows = {r["uuid"]: r for r in contract_rows(rep, ["none", "plan"], date(2023, 5, 14))}
    assert rows["none"]["time_basis"] == "none" and rows["none"]["time_window_start"] is None
    assert rows["plan"]["time_planned_start"] == "2023-06-01"
    assert rows["plan"]["time_outcome"] == "plan"


def test_expression_truncated_and_offset_is_stable_json() -> None:
    offset = {"unit": "week", "amount": 2, "sign": -1}
    rows = contract_rows(report(result("e", expression="x" * 500, anchor_offset=offset)), ["e"], None)
    assert len(rows[0]["time_expression"]) == 120
    assert json.loads(rows[0]["time_anchor_offset"]) == offset
    assert rows[0]["time_anchor_offset"] == json.dumps(offset, sort_keys=True, separators=(",", ":"))


def test_statement_is_write_once_episode_bound_and_tenant_scoped() -> None:
    assert "r.time_contract IS NULL" in PERSIST_CYPHER
    assert "$episode_uuid IN coalesce(r.episodes, [])" in PERSIST_CYPHER
    assert "r.group_id = $group_id" in PERSIST_CYPHER
    assert "$tenant_namespaces IS NULL OR" in PERSIST_CYPHER
    assert "invalid_at" not in PERSIST_CYPHER and "expired_at" not in PERSIST_CYPHER
    assert "r.valid_at" not in PERSIST_CYPHER


# --- persist ----------------------------------------------------------------------------------


async def test_persist_sends_rows_with_scope_params() -> None:
    driver, rep = FakeDriver(), report(result("e1"))
    await persist_anchored_time(driver, rep, episode_uuid="ep", group_id="", edge_uuids=["e1"],
                                speech_date=date(2023, 5, 14))
    (_query, params), = driver.calls
    assert params["episode_uuid"] == "ep" and params["group_id"] == ""
    assert sorted(params["tenant_namespaces"]) == ["", "default"]
    assert rep.persist == "ok" and rep.persisted == 1


async def test_nothing_to_write_makes_no_call() -> None:
    driver, rep = FakeDriver(), report(result("e1"))
    await persist_anchored_time(driver, rep, episode_uuid="ep", group_id="g", edge_uuids=["other"],
                                speech_date=None)
    assert driver.calls == [] and rep.persist == "no_rows"
    await persist_anchored_time(driver, rep, episode_uuid=None, group_id="g", edge_uuids=["e1"],
                                speech_date=None)
    assert driver.calls == [] and rep.persist == "no_episode"


async def test_write_failure_never_raises() -> None:
    rep = report(result("e1"))
    await persist_anchored_time(FakeDriver(RuntimeError("neo4j down")), rep, episode_uuid="ep",
                                group_id="g", edge_uuids=["e1"], speech_date=None)
    assert rep.persist == "error"


async def test_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await persist_anchored_time(FakeDriver(asyncio.CancelledError()), report(result("e1")),
                                    episode_uuid="ep", group_id="g", edge_uuids=["e1"],
                                    speech_date=None)


# --- GraphitiClient.add_episode wiring --------------------------------------------------------


def _client(produce=None, *, edges=("e1",), driver=None):
    """A native Graphiti stand-in; ``produce(receipt)`` plays the extraction hook."""
    native = AsyncMock()
    native.driver = driver or FakeDriver()

    async def add_episode(**kwargs):
        receipt = extraction.get_extraction_receipt()
        if produce is not None and receipt is not None:
            produce(receipt)
        return SimpleNamespace(episode=SimpleNamespace(uuid="ep-1"),
                               edges=[SimpleNamespace(uuid=u) for u in edges])

    native.add_episode = add_episode
    return GraphitiClient(client=native), native.driver


async def _add(client) -> object:
    return await client.add_episode(name="t", episode_body="user: a month ago I moved",
                                    source_description="user", reference_time=NOW, group_id="g")


@pytest.fixture
def receipt():
    rec = extraction.begin_extraction_receipt("k", "user: a month ago I moved")
    yield rec
    extraction.clear_extraction_receipt()


async def test_new_edges_persisted_duplicates_skipped(receipt) -> None:
    def produce(rec):  # "dup" was resolved to an existing edge, so it is not in the result
        rec.anchored_time = report(result("e1"), result("dup", fact_index=1))
    client, driver = _client(produce, edges=("e1", "existing-edge"))
    await _add(client)
    (_query, params), = driver.calls
    assert [r["uuid"] for r in params["rows"]] == ["e1"]
    assert params["episode_uuid"] == "ep-1" and params["group_id"] == "g"
    assert params["rows"][0]["time_speech_date"] == "2023-05-14"


async def test_speech_date_is_the_utc_date_of_the_reference_time(receipt) -> None:
    def produce(rec):
        rec.anchored_time = report(result("e1"))
    client, driver = _client(produce)
    late_evening = datetime(2023, 5, 14, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    await client.add_episode(name="t", episode_body="x", source_description="user",
                             reference_time=late_evening, group_id="g")
    assert driver.calls[0][1]["rows"][0]["time_speech_date"] == "2023-05-15"


async def test_stale_report_from_an_earlier_call_is_not_persisted(receipt) -> None:
    receipt.anchored_time = report(result("e1"))  # left over; this call's hook did not run
    client, driver = _client(None)
    await _add(client)
    assert driver.calls == []


@pytest.mark.parametrize("status", ["skipped", "timeout", "internal_error", "pending"])
async def test_non_ok_report_is_not_persisted(receipt, status) -> None:
    def produce(rec):
        rec.anchored_time = report(result("e1"), status=status)
    client, driver = _client(produce)
    await _add(client)
    assert driver.calls == []


async def test_resolver_off_means_no_write(receipt) -> None:
    client, driver = _client(None)
    await _add(client)
    assert receipt.anchored_time is None and driver.calls == []


async def test_persist_failure_does_not_fail_add_episode(receipt) -> None:
    def produce(rec):
        rec.anchored_time = report(result("e1"))
    client, _driver = _client(produce, driver=FakeDriver(RuntimeError("boom")))
    out = await _add(client)
    assert out.episode.uuid == "ep-1"
    assert receipt.anchored_time.persist == "error"
