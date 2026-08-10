"""Toggleable consolidation audit trail -- pure, offline.

The audit is behavior-neutral: OFF by default, a no-op that never records; ON, it writes one
structured lifecycle event per decision to the telemetry store under component='consolidation_audit',
readable back in oldest-first replay order. Emission must NEVER raise into the caller.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from menhir.infrastructure import consolidation_audit as A
from menhir.infrastructure.telemetry import record_lifecycle_event
from menhir.infrastructure.telemetry.store import McpTelemetryStore


@pytest.fixture
def store():
    db = Path(tempfile.mkdtemp()) / "audit.db"
    return McpTelemetryStore(db)


@pytest.fixture(autouse=True)
def _restore_toggle():
    before = A.is_enabled()
    try:
        yield
    finally:
        A.set_enabled(before)


def test_default_off_is_noop(store):
    A.set_enabled(False)
    A.audit("fold", "abstain", pass_id="cap-x", details={"reason": "AMBIGUOUS_ANCHOR"})
    assert A.read_pass("cap-x", store=store) == []


def test_off_emit_never_raises_even_with_unserializable():
    A.set_enabled(False)
    # a non-JSON object must not raise even if it were serialized; OFF short-circuits before that.
    A.audit("view_write", "created", details={"obj": object()})


def test_on_emit_never_raises_with_unserializable(store, monkeypatch):
    # Force the module default store to the throwaway; ON path with junk must still not raise.
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.telemetry_store", store, raising=False)
    A.set_enabled(True)

    def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(store, "record_lifecycle_event", _boom, raising=False)
    # Even if the underlying recorder blows up, audit() swallows it.
    A.audit("fold", "anchor", pass_id="cap-boom", details={"obj": object()})


def test_on_records_shape_and_replay_order(store):
    A.set_enabled(True)
    pid = "cap-order"
    for ev, st in [("pass", "start"), ("perceive", "bound"), ("fold", "abstain"),
                   ("view_write", "stale_skipped"), ("reconcile_retire", "abstain")]:
        record_lifecycle_event(
            component=A.COMPONENT, event=ev, state=st, episode_uuid=pid,
            details={"slot": ["owned", "", "count", ""]}, store=store)
    rows = A.read_pass(pid, store=store)
    assert [(r["event"], r["state"]) for r in rows] == [
        ("pass", "start"), ("perceive", "bound"), ("fold", "abstain"),
        ("view_write", "stale_skipped"), ("reconcile_retire", "abstain"),
    ]
    assert all(r["component"] == "consolidation_audit" for r in rows)


def test_begin_pass_sets_ambient_id():
    pid = A.begin_pass()
    assert pid.startswith("cap-")
    assert A.current_pass_id() == pid
    given = A.begin_pass("cap-explicit")
    assert given == "cap-explicit" and A.current_pass_id() == "cap-explicit"


def test_read_pass_isolates_by_pass_id(store):
    A.set_enabled(True)
    for pid in ("cap-a", "cap-b"):
        record_lifecycle_event(component=A.COMPONENT, event="rebuild", state="done",
                               episode_uuid=pid, details=None, store=store)
    assert len(A.read_pass("cap-a", store=store)) == 1
    assert len(A.read_pass("cap-b", store=store)) == 1
    assert len(A.read_recent(store=store)) == 2



def _capture(monkeypatch):
    """Capture what audit() actually emits.

    audit() calls the module-level record_lifecycle_event WITHOUT a store argument, so it always
    writes to the default store -- pointing `telemetry_store` at a temp DB does not redirect it.
    Patching the function in the audit module's namespace is the seam that observes the real path.
    """
    seen: list[dict] = []

    def _rec(*, component, event, state, episode_uuid=None, details=None, **kw):
        seen.append({"component": component, "event": event, "state": state,
                     "pass_id": episode_uuid, "details": details})

    monkeypatch.setattr(
        "menhir.infrastructure.consolidation_audit.record_lifecycle_event", _rec, raising=True)
    return seen


def test_vetoed_gate_decisions_are_audited(monkeypatch):
    """A vetoed claim must leave a trace saying WHY the k samples disagreed.

    Before this emit existed, only claims that already WON were explained: the `perceive` event
    fires after record_assertion, so an abstention -- by far the most common outcome -- was
    invisible after the fact. `distribution` is the load-bearing field; without it the reason for
    disagreement is recoverable only by re-running extraction against a live LLM.
    """
    from menhir.services.typed_scalar_perception import (
        TypedScalarDecision, bind_and_persist_typed_scalars, _VOTE_ABSENT)

    seen = _capture(monkeypatch)
    A.set_enabled(True)

    vetoed = TypedScalarDecision(
        source_key="ep-1:10:20:0", committed=False,
        reason="scattered: modal interpretation holds 2/3 (threshold 1.0)",
        veto="self_consistency", agreement=2 / 3, k=3,
        distribution={"userteam_sizecountabsolute5": 2, _VOTE_ABSENT: 1},
        proposal=None,
    )
    out = bind_and_persist_typed_scalars(
        [vetoed],
        linked_entities_for_episode=lambda _u: [],
        record_assertion=lambda _a: {},
        rebuild_scalar_state=lambda _u: None,
        namespace="ns-test",
    )
    # behaviour is unchanged: a vetoed decision still persists nothing
    assert out["persisted"] == 0 and out["bound"] == 0

    assert [(e["event"], e["state"]) for e in seen] == [("gate", "self_consistency")]
    d = seen[0]["details"]
    assert d["agreement"] == pytest.approx(2 / 3) and d["k"] == 3
    assert d["source_key"] == "ep-1:10:20:0" and d["namespace"] == "ns-test"
    # control characters are rendered readable, and the sentinel is named rather than raw
    assert d["distribution"] == {"user|team_size||count||absolute|5|": 2, "(absent)": 1}


def test_gate_audit_is_noop_when_disabled(monkeypatch):
    """The whole trail is opt-in; a vetoed decision must record nothing when the toggle is OFF."""
    from menhir.services.typed_scalar_perception import (
        TypedScalarDecision, bind_and_persist_typed_scalars)

    seen = _capture(monkeypatch)
    A.set_enabled(False)
    bind_and_persist_typed_scalars(
        [TypedScalarDecision(source_key="s", committed=False, reason="r", veto="self_consistency",
                             agreement=0.0, k=3, distribution={}, proposal=None)],
        linked_entities_for_episode=lambda _u: [],
        record_assertion=lambda _a: {},
        rebuild_scalar_state=lambda _u: None,
        namespace="ns-test",
    )
    assert seen == []


def test_parse_stage_drops_are_attributable(monkeypatch):
    """A row the model emitted but we discarded must be distinguishable from one never emitted.

    Downstream the two look identical -- which is exactly what made the measured 72% "absence" band
    uninterpretable. `on_drop` is an injected seam so the function stays pure when it is omitted.
    """
    import json
    from collections import Counter
    from menhir.services.typed_scalar_perception import extract_typed_scalars_once

    class _Ep:
        def __init__(self, uuid, content):
            self.uuid, self.content = uuid, content

    ep = [_Ep("e1", "[2026-07-23] I have 37 coins. I have about 30 books. I said it. I said it.")]
    rows = [
        {"episode": 0, "subject": "user", "attribute": "coins", "operation": "absolute",
         "value_kind": "count", "value": 37, "stated_span": "I have 37 coins"},
        # hedged -> must not become an exact scalar
        {"episode": 0, "subject": "user", "attribute": "books", "operation": "absolute",
         "value_kind": "count", "value": 30, "stated_span": "I have about 30 books"},
        # span occurs twice -> not uniquely locatable
        {"episode": 0, "subject": "user", "attribute": "z", "operation": "absolute",
         "value_kind": "count", "value": 1, "stated_span": "I said it."},
        # not a number for a count kind
        {"episode": 0, "subject": "user", "attribute": "q", "operation": "absolute",
         "value_kind": "count", "value": "not-a-number", "stated_span": "I have 37 coins"},
        # a dict missing every required field, and a row that is not an object at all --
        # different failures, and easy to conflate: {"not": "a dict"} IS a dict.
        {"episode": 0, "subject": "user"},
        "not-an-object-at-all",
    ]
    drops: Counter = Counter()
    out = extract_typed_scalars_once(
        ep, lambda _s, _u: json.dumps(rows), on_drop=lambda r: drops.update((r,)))

    assert [(p.attribute, p.normalized_value) for p in out] == [("coins", "37")]
    assert dict(drops) == {
        "hedged_value": 1,
        "span_not_uniquely_located": 1,
        "value_failed_validation": 1,
        "missing_required_field": 1,
        "not_an_object": 1,
    }


def test_extract_audit_preserves_bounded_per_sample_proposal_identity(monkeypatch):
    """An audit replay must distinguish omission from quote-boundary/source-key fragmentation."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from menhir.services import typed_scalar_service as service_module
    from menhir.services.typed_scalar_perception import TypedScalarProposal

    seen = _capture(monkeypatch)
    A.set_enabled(True)
    proposal = TypedScalarProposal(
        subject_text="user",
        attribute="completed_lessons",
        scope="course",
        value_kind="count",
        unit="",
        operation="absolute",
        value=30,
        stated_span="30 lessons",
        episode_uuid="episode-1",
        span_start=17,
        span_end=27,
    )
    monkeypatch.setattr(
        service_module, "extract_typed_scalars_once",
        lambda *args, **kwargs: [proposal],
    )
    monkeypatch.setattr(service_module, "gate_typed_scalars", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        service_module,
        "bind_and_persist_typed_scalars",
        lambda *args, **kwargs: {"bound": 0},
    )

    service = service_module.TypedScalarPerceptionService.__new__(
        service_module.TypedScalarPerceptionService
    )
    monkeypatch.setattr(type(service), "ensure_activated", lambda self: None)
    monkeypatch.setattr(type(service), "_make_self_seam", lambda self: (lambda _ns: None))
    service._adapter = MagicMock()
    service._service = MagicMock()
    service._perceiver_version = "test"
    service._embed = None
    service._embed_version = None
    service._scalar_history_enabled = False

    service.perceive_and_persist(
        [SimpleNamespace(uuid="episode-1", content="I have completed 30 lessons")],
        lambda *_args: "[]",
        k=2,
        namespace="ns-audit",
    )

    extract = next(event for event in seen if event["event"] == "extract")
    details = extract["details"]
    assert [sample["kept"] for sample in details["samples"]] == [1, 1]
    summary = details["samples"][0]["proposals"][0]
    assert summary == {
        "source_key": proposal.source_key,
        "episode_uuid": "episode-1",
        "span_start": 17,
        "span_end": 27,
        "attribute": "completed_lessons",
        "scope": "course",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": "30",
        "when": None,
    }
    assert "stated_span" not in summary


def test_unreadable_response_is_distinguishable_from_genuine_silence():
    """A pass that could not be PARSED is not a pass that found nothing.

    Both yield zero rows, so before this they were the same event downstream. They are not the same:
    a well-formed `[]` is one sample voting "no claim here", while a truncated array (max_tokens=512
    at production settings, and a cut-off array never closes) is a LOST sample -- at threshold=1.0 it
    silently vetoes every claim in the batch by diluting the denominator. Attributing it is the
    difference between "the model disagrees" and "we never heard the model".
    """
    from collections import Counter
    from menhir.services.typed_scalar_perception import extract_typed_scalars_once

    class _Ep:
        def __init__(self, uuid, content):
            self.uuid, self.content = uuid, content

    ep = [_Ep("e1", "[2026-07-23] I have 37 coins.")]

    def _run(response: str) -> dict:
        drops: Counter = Counter()
        out = extract_typed_scalars_once(
            ep, lambda _s, _u: response, on_drop=lambda r: drops.update((r,)))
        assert out == []
        return dict(drops)

    # the model legitimately found nothing -- silence, and NOT a defect to report
    assert _run("[]") == {}
    # truncated mid-array: the realistic max_tokens failure
    assert _run('[{"episode": 0, "subject": "user", "attribute": "co') == {"malformed_json": 1}
    # valid JSON, wrong shape
    assert _run('{"episode": 0}') == {"not_a_json_array": 1}
    assert _run("I could not find any scalar claims.") == {"malformed_json": 1}


def test_consolidation_completion_budget_clears_measured_demand():
    """The extractor's completion cap must exceed real measured demand, with headroom.

    The extractor emits every claim it found in ONE JSON array, so this cap bounds CLAIMS PER CALL.
    A cut-off array never closes, so exceeding it does not lose the tail -- it loses the whole
    response (unparseable -> zero rows), and at threshold=1.0 that lost sample then votes `absent`
    against every claim the other samples found.

    Measured on the LME corpus (19 namespaces x k=3, unconstrained 8000 budget): p50 ~330, max 931.
    Demand is stochastic -- one namespace was observed at 761 and 931 on repeat runs of identical
    input at temp 0.7 -- so the cap is sized against the max plus margin, not against the median.
    Acceptance sweep at 2048: 0 truncated / 54 calls, highest completion 877.

    This test pins the floor so the cap cannot be quietly walked back to 512, which lost 3/19
    namespaces outright.
    """
    from menhir.config.settings_model import MemorySettings

    cap = MemorySettings.personal_memory_consolidation_max_tokens
    assert cap >= 1500, (
        f"completion cap {cap} is below measured LME demand (max 931 observed) plus margin; "
        "at 512 three of nineteen namespaces emitted ZERO claims"
    )


def test_extract_stays_pure_without_the_seam():
    """Omitting on_drop must change nothing -- 25 existing call sites rely on the pure contract."""
    import json
    from menhir.services.typed_scalar_perception import extract_typed_scalars_once

    class _Ep:
        def __init__(self, uuid, content):
            self.uuid, self.content = uuid, content

    ep = [_Ep("e1", "[2026-07-23] I have 37 coins.")]
    rows = [{"episode": 0, "subject": "user", "attribute": "coins", "operation": "absolute",
             "value_kind": "count", "value": 37, "stated_span": "I have 37 coins"},
            {"bad": "row"}]
    out = extract_typed_scalars_once(ep, lambda _s, _u: json.dumps(rows))
    assert [(p.attribute, p.normalized_value) for p in out] == [("coins", "37")]
