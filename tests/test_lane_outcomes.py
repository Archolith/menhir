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
    _NO_LANE_KEYS,
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
    assert "counter" not in out
    assert "event" not in out
    assert out["counting"] == {"enabled": False}
    assert out["scalar"]["enabled"] is True


def test_lane_that_read_input_but_produced_nothing_is_flagged():
    out = lane_outcomes(
        {"scalar_namespaces_dirty": 1, "scalar_namespaces_processed": 1, "scalar_evidence_rows": 4,
         "scalar_decisions": 2, "scalar_assertions_persisted": 0},
        enabled={"scalar": True})
    assert out["scalar"]["no_output"] is True and out["scalar"]["stalled"] is False
    assert out["scalar"]["stages"]["decisions"] == 2
    assert out["scalar"]["namespaces_failed"] == 0


def test_lane_with_dirty_namespaces_it_never_processed_is_stalled():
    out = lane_outcomes({"scalar_namespaces_dirty": 2, "scalar_namespaces_processed": 0},
                        enabled={"scalar": True})
    assert out["scalar"]["stalled"] is True and out["scalar"]["no_output"] is False


def test_lane_with_no_input_is_neither_flag():
    out = lane_outcomes({}, enabled={"counting": True})
    assert out["counting"]["no_output"] is False and out["counting"]["stalled"] is False


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

def test_scalar_success_reports_beside_disabled_lanes():
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0")},
        entities_by_ep={"e0": [{"uuid": "ent-user", "name": "user"}]},
    )
    lanes = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=3, enable_scalar_state=True))["lane_outcomes"]
    assert lanes["scalar"]["output"] == 1 and lanes["scalar"]["no_output"] is False
    assert lanes["counting"] == {"enabled": False}
    assert "event" not in lanes and "counter" not in lanes


def test_no_lane_pass_keeps_its_exact_shape():
    graph = _FakeGraph(dirty=["lme-a"], episodes_by_ns={"lme-a": _eps("I spent forty dollars")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=1))
    assert set(out) == _NO_LANE_KEYS
