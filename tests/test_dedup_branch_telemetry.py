"""Resolution-branch telemetry, re-homed on the fork's supported hook callbacks.

The former installer #13 wrapped the resolver's private similarity helper; the fork
owns that boundary and exposes no private seam by design. Menhir reconstructs the
same visibility — per-node pre-resolution, per-candidate pool composition and
exclusions, and per-merge LLM outcomes — at the supported hook boundaries, and
flushes it once per ``add_episode`` call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_resolution_policy import (
    MenhirCandidateFilterHook,
    MenhirIdentityGateHook,
    ResolutionTelemetry,
    flush_resolution_telemetry,
    start_resolution_telemetry,
)
from graphiti_core.candidate_filter import CandidateFilterDecision
from graphiti_core.identity_gate import IdentityGateDecision


def _node(name: str, uuid: str = "x", **attributes):
    from graphiti_core.nodes import EntityNode

    return EntityNode(
        uuid=uuid, name=name, group_id="", labels=["Entity"],
        created_at=datetime.now(timezone.utc),
        attributes=dict(attributes),
    )


def _gate_context(extracted, candidate, *, episode=None, edges=None):
    return SimpleNamespace(
        extracted_node=extracted,
        candidate_node=candidate,
        candidate_id=0,
        episode=episode,
        previous_episodes=[],
        edges=list(edges or []),
    )


class _FakeEdge:
    def __init__(self, fact: str) -> None:
        self.fact = fact


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_filter_counts_pools_and_exclusions():
    start_resolution_telemetry()
    hook = MenhirCandidateFilterHook()
    extracted = _node("coins", "e1")
    structural = _node("scoring_service.py", "c1", structure_role="file")
    view = _node("alice's coins: 37", "c2", is_view=True)
    semantic = _node("Alice's coins", "c3")

    for candidate in (structural, view, semantic):
        await hook.filter_candidate(SimpleNamespace(extracted_node=extracted, candidate_node=candidate))

    collector = SimpleNamespace()  # placeholder to keep naming clear
    del collector

    from menhir.infrastructure.graphiti_resolution_policy import (
        _resolution_telemetry,
    )

    telemetry = _resolution_telemetry.get()
    details = telemetry.as_details()
    assert details["structural_excluded"] == 1
    assert details["view_excluded"] == 1
    assert details["candidate_count_max"] == 1
    assert details["no_candidates_new"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identity_gate_counts_merges_and_vetoes():
    start_resolution_telemetry()
    hook = MenhirIdentityGateHook()
    extracted = _node("the suburbs", "e1")
    chicago = _node("Chicago", "c1")
    metro = _node("NYC metro area", "c2")
    nyc = _node("NYC", "e2")

    assert await hook.evaluate_identity_gate(_gate_context(extracted, chicago)) is IdentityGateDecision.VETO
    assert await hook.evaluate_identity_gate(_gate_context(nyc, metro)) is IdentityGateDecision.ALLOW

    from menhir.infrastructure.graphiti_resolution_policy import _resolution_telemetry

    details = _resolution_telemetry.get().as_details()
    assert details["identity_gate_vetoes"] == 1
    assert details["llm_selected_candidate"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_consistency_veto_survives_the_hook_path():
    """Fact text mentioning the extracted name but not the candidate contradicts the merge."""
    hook = MenhirIdentityGateHook()
    extracted = _node("the suburbs", "e1")
    candidate = _node("suburbs park", "c1")  # name-overlap evidence exists...

    # ...but the episode's edges mention "the suburbs" and never the candidate.
    decision = await hook.evaluate_identity_gate(
        _gate_context(extracted, candidate, edges=[_FakeEdge("Rachel moved to the suburbs.")])
    )

    assert decision is IdentityGateDecision.VETO


@pytest.mark.unit
def test_flush_records_one_outcomes_event(monkeypatch):
    from menhir.infrastructure.graphiti_resolution_policy import _resolution_telemetry
    import menhir.infrastructure.telemetry.recorders as recorders

    recorded: list[dict] = []

    def _capture(*, component, event, state, episode_uuid=None, details=None, **kw):
        recorded.append({"component": component, "event": event, "details": details or {}})

    monkeypatch.setattr(recorders, "record_lifecycle_event", _capture)

    telemetry = ResolutionTelemetry()
    telemetry.extracted_node_count = 3
    telemetry.pre_resolved_self = 1
    telemetry.identity_gate_merges = 2
    telemetry.identity_gate_vetoes = 1
    _resolution_telemetry.set(telemetry)

    flush_resolution_telemetry()

    assert len(recorded) == 1
    assert recorded[0]["component"] == "graphiti_dedup"
    assert recorded[0]["event"] == "resolution_outcomes"
    d = recorded[0]["details"]
    assert d["extracted_node_count"] == 3
    assert d["pre_resolved_self"] == 1
    assert d["llm_selected_candidate"] == 2
    assert d["identity_gate_vetoes"] == 1
    assert _resolution_telemetry.get() is None, "flush must reset the collector"


@pytest.mark.unit
def test_flush_never_raises_and_is_a_noop_without_a_collector(monkeypatch):
    import menhir.infrastructure.telemetry.recorders as recorders

    def _boom(**kw):
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr(recorders, "record_lifecycle_event", _boom)

    flush_resolution_telemetry()  # no collector: no-op
    from menhir.infrastructure.graphiti_resolution_policy import _resolution_telemetry

    _resolution_telemetry.set(ResolutionTelemetry())
    flush_resolution_telemetry()  # recorder raises: swallowed, collector still reset
    assert _resolution_telemetry.get() is None
