"""CF-121: a verifier value change must not lose its belief-flag to a mid-sequence failure.

`changed` is an EDGE-TRIGGERED signal: computed from `prev` before the write, consumed after it.
`record_counter` supersedes `prev`. So once the new value is durable, the condition that produced
the signal no longer exists, and no later sync can recompute it -- on the next pass
`prev["value"] == res.value`, `changed` is False, and nothing retries. There is no journal row, no
retry marker, and no reconciliation.

The module docstring states exactly what that loses: "on a value change they are flagged for review
so recall can down-rank prose that now restates a stale value."

These tests simulate the dangerous interleavings directly: fail at each write step AFTER the
decision point and assert the flag either already happened or is still reachable on the next sync.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services.verifier_sync import VerifierContext, sync_verifiers


class _Graph:
    """Counter store whose `record_counter` can be made to fail, and which -- crucially --
    only supersedes the previous value when that call SUCCEEDS."""

    def __init__(self, existing: float | None = None, fail_record: bool = False) -> None:
        self.existing = existing
        self.fail_record = fail_record
        self.records: list[dict[str, Any]] = []

    def fetch_counter(self, *, subject, counter, namespace=None):
        if self.existing is None:
            return None
        return {"subject": subject, "counter": counter, "value": self.existing}

    def record_counter(self, **kwargs):
        if self.fail_record:
            raise RuntimeError("neo4j write failed")
        self.records.append(kwargs)
        self.existing = kwargs["value"]  # the supersession that destroys the trigger condition
        return {"uuid": "reg-uuid", "view_key": "k", "created": True}


class _Repo:
    def __init__(self, verifiers, *, fail_edge=False, fail_stamp=False) -> None:
        self._verifiers = verifiers
        self.fail_edge = fail_edge
        self.fail_stamp = fail_stamp
        self.flag_calls: list[dict[str, Any]] = []
        self.edges: list[tuple[str, str]] = []
        self.stamps: list[dict[str, Any]] = []

    def list_verifiers(self):
        return self._verifiers

    def ensure_verified_edge(self, *, register_uuid, verifier_uuid):
        if self.fail_edge:
            raise RuntimeError("ensure_verified_edge failed")
        self.edges.append((register_uuid, verifier_uuid))

    def stamp_verifier(self, *, verifier_uuid, value, display, at):
        if self.fail_stamp:
            raise RuntimeError("stamp_verifier failed")
        self.stamps.append({"uuid": verifier_uuid, "value": value})

    def flag_referencing_beliefs(self, *, verifier_uuid, new_value, display, at):
        self.flag_calls.append({"uuid": verifier_uuid, "value": new_value})
        return 3


def _verifier():
    return {
        "uuid": "v1",
        "verifier_kind": "env_key",
        "verifier_params": '{"key":"MENHIR_TEST_FLAG"}',
        "register_subject": "menhir-config",
        "register_counter": "experience_counter_enabled",
    }


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch):
    """The verifier reads this env key; 'true' -> 1.0, so a stored 0.0 means changed."""
    monkeypatch.setenv("MENHIR_TEST_FLAG", "true")


@pytest.mark.unit
def test_ensure_verified_edge_failure_does_not_lose_the_flag() -> None:
    """The exact interleaving in the finding: the value lands, then a Neo4j error."""
    graph = _Graph(existing=0.0)
    repo = _Repo([_verifier()], fail_edge=True)

    with pytest.raises(RuntimeError):
        sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert repo.flag_calls, "the flag was lost -- it must be issued before the write can fail"


@pytest.mark.unit
def test_stamp_verifier_failure_does_not_lose_the_flag() -> None:
    graph = _Graph(existing=0.0)
    repo = _Repo([_verifier()], fail_stamp=True)

    with pytest.raises(RuntimeError):
        sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert repo.flag_calls


@pytest.mark.unit
def test_a_failed_record_leaves_the_change_still_detectable_next_sync() -> None:
    """If the write itself fails, `prev` must be untouched so the NEXT sync still sees a change.

    This is what makes the ordering safe rather than merely earlier: the signal stays
    reconstructible from durable state.
    """
    graph = _Graph(existing=0.0, fail_record=True)
    repo = _Repo([_verifier()])

    with pytest.raises(RuntimeError):
        sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert graph.existing == 0.0, "prev was superseded despite the write failing"

    graph.fail_record = False
    repo2 = _Repo([_verifier()])
    out = sync_verifiers(repo=repo2, graph_adapter=graph, context=VerifierContext())

    assert out[0]["changed"] is True
    assert repo2.flag_calls, "the retry did not reissue the flag"


@pytest.mark.unit
def test_the_flag_precedes_the_record_in_call_order() -> None:
    """Structural: assert the ordering directly, so a later refactor cannot quietly restore the
    edge-triggered-after-supersession shape while the failure tests still pass."""
    order: list[str] = []

    graph = _Graph(existing=0.0)
    repo = _Repo([_verifier()])

    real_record = graph.record_counter
    real_flag = repo.flag_referencing_beliefs

    def record(**kwargs):
        order.append("record")
        return real_record(**kwargs)

    def flag(**kwargs):
        order.append("flag")
        return real_flag(**kwargs)

    graph.record_counter = record
    repo.flag_referencing_beliefs = flag

    sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert order == ["flag", "record"], f"flag must precede record; got {order}"


@pytest.mark.unit
def test_an_unchanged_value_still_flags_nothing() -> None:
    """POSITIVE CONTROL: the reorder must not make flagging unconditional. Without this, every
    test above would pass against a sync that flags on every pass."""
    graph = _Graph(existing=1.0)  # already 'true' -> unchanged
    repo = _Repo([_verifier()])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert out[0]["changed"] is False
    assert repo.flag_calls == []
    assert graph.records, "the register must still be re-recorded on an unchanged pass"


@pytest.mark.unit
def test_the_happy_path_still_records_links_stamps_and_flags() -> None:
    """POSITIVE CONTROL: everything downstream of the flag must still run in the normal case."""
    graph = _Graph(existing=0.0)
    repo = _Repo([_verifier()])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert out[0]["status"] == "refreshed"
    assert out[0]["changed"] is True
    assert out[0]["beliefs_flagged"] == 3
    assert len(graph.records) == 1 and graph.records[0]["value"] == 1.0
    assert repo.edges == [("reg-uuid", "v1")]
    assert repo.stamps and repo.stamps[0]["value"] == 1.0
