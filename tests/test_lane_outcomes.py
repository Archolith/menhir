"""#170 item 4: per-stage scalar counts and a per-lane outcome report on the consolidation pass.

One lane's output must not hide another's inactivity: every enabled lane is reported on its own,
with what it read, what each stage produced, and no_output / stalled flags.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.services import scheduler_tasks
from menhir.services.lane_outcomes import lane_outcomes
from tests.test_consolidate_personal_memory import (
    _COUNTER_ONLY_KEYS,
    _FakeGraph,
    _ScalarGraph,
    _empty_llm,
    _eps,
    _wake_eps,
    _wake_llm,
)

pytestmark = pytest.mark.unit


# ---- the report builder -------------------------------------------------------------------------

def test_disabled_lanes_are_listed_as_disabled():
    out = lane_outcomes({}, enabled={"scalar": True})
    assert out["counter"] == {"enabled": False}
    assert out["event"] == {"enabled": False}
    assert out["counting"] == {"enabled": False}
    assert out["scalar"]["enabled"] is True


def test_lane_that_read_input_but_produced_nothing_is_flagged():
    out = lane_outcomes(
        {"event_namespaces_dirty": 1, "event_namespaces_processed": 1, "event_evidence_rows": 4,
         "event_proposals": 2, "event_assertions_recorded": 0},
        enabled={"event": True})
    assert out["event"]["no_output"] is True and out["event"]["stalled"] is False
    assert out["event"]["stages"]["proposals"] == 2
    assert out["event"]["namespaces_failed"] == 0


def test_lane_with_dirty_namespaces_it_never_processed_is_stalled():
    out = lane_outcomes({"scalar_namespaces_dirty": 2, "scalar_namespaces_processed": 0},
                        enabled={"scalar": True})
    assert out["scalar"]["stalled"] is True and out["scalar"]["no_output"] is False


def test_lane_with_no_input_is_neither_flag():
    out = lane_outcomes({}, enabled={"counting": True})
    assert out["counting"]["no_output"] is False and out["counting"]["stalled"] is False


def test_counter_rows_come_from_the_argument_not_the_result():
    out = lane_outcomes({"namespaces_dirty": 1, "namespaces_processed": 1, "views_written": 0},
                        enabled={"counter": True}, counter_evidence_rows=3)
    assert out["counter"]["input_rows"] == 3 and out["counter"]["no_output"] is True


# ---- scalar stage counts from a real scalar pass over the fake graph ------------------------------

def test_scalar_pass_reports_each_stage():
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0")},
        entities_by_ep={"e0": [{"uuid": "ent-user", "name": "user"}]},
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=3, enable_scalar_state=True))

    assert out["scalar_evidence_rows"] == 1
    assert out["scalar_decisions"] == 1 and out["scalar_assertions_committed"] == 1
    assert out["scalar_assertions_persisted"] == 1
    assert out["scalar_states_written"] == 1          # bound
    assert out["scalar_binding_mismatched"] == 0
    assert out["scalar_views_rebuilt"] == 1
    scalar = out["lane_outcomes"]["scalar"]
    assert scalar["stages"] == {"decisions": 1, "committed": 1, "persisted": 1, "bound": 1,
                                "binding_mismatched": 0, "advisory": 0, "views_rebuilt": 1}
    assert scalar["output"] == 1 and scalar["no_output"] is False


def test_unbindable_scalar_claim_is_persisted_but_not_bound_or_rebuilt():
    graph = _ScalarGraph(scalar_dirty=["lme-a"], episodes_by_ns={"lme-a": _wake_eps("e0")},
                         entities_by_ep={})
    graph.ensure_self_entity = lambda namespace: None  # no self entity: the claim stays advisory
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=3, enable_scalar_state=True))
    stages = out["lane_outcomes"]["scalar"]["stages"]
    assert stages["persisted"] == 1 and stages["bound"] == 0 and stages["views_rebuilt"] == 0
    assert stages["advisory"] == 1


# ---- the pass report keeps lanes apart -----------------------------------------------------------

def _event_runner(recorded: int):
    def _runner(graph_adapter, *, event_targets, counting_llm, call_budget, config):
        return {"event_namespaces_dirty": len(event_targets),
                "event_namespaces_processed": len(event_targets), "event_namespaces_failed": 0,
                "event_batches": 1, "event_evidence_rows": 2, "event_proposals": recorded,
                "event_proposals_admitted": recorded, "event_assertions_recorded": recorded,
                "event_views_rebuilt": 1 if recorded else 0, "event_llm_calls": 0}
    return _runner


def _event_pass(monkeypatch, recorded: int) -> dict:
    async def _select(*_a, **_kw):
        return ["ns-a"]

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _select)
    monkeypatch.setattr(scheduler_tasks, "run_event_consolidation", _event_runner(recorded))
    graph = _FakeGraph(dirty=["ns-a"], episodes_by_ns={"ns-a": _eps("I spent forty dollars")})
    return asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=1, enable_event_history=True))


def test_event_success_does_not_hide_an_idle_counter(monkeypatch):
    lanes = _event_pass(monkeypatch, recorded=2)["lane_outcomes"]
    assert lanes["event"]["output"] == 2 and lanes["event"]["no_output"] is False
    assert lanes["counter"]["input_rows"] == 1 and lanes["counter"]["no_output"] is True
    assert lanes["scalar"] == {"enabled": False} and lanes["counting"] == {"enabled": False}


def test_idle_event_lane_is_flagged_beside_the_counter(monkeypatch):
    lanes = _event_pass(monkeypatch, recorded=0)["lane_outcomes"]
    assert lanes["event"]["no_output"] is True and lanes["event"]["input_rows"] == 2


def test_counter_only_pass_keeps_its_exact_shape():
    graph = _FakeGraph(dirty=["lme-a"], episodes_by_ns={"lme-a": _eps("I spent forty dollars")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=1))
    assert set(out) == _COUNTER_ONLY_KEYS
