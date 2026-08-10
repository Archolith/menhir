"""Generic AuditChannel -- pure, offline.

A channel is behavior-neutral: OFF by default, a no-op that never records; ON, it writes one
structured lifecycle event per decision under its component, readable back in oldest-first replay
order. Emission must NEVER raise into the caller, and two channels are independent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from menhir.infrastructure.audit_trail import RECALL, AuditChannel
from menhir.infrastructure.telemetry import record_lifecycle_event
from menhir.infrastructure.telemetry.store import McpTelemetryStore


@pytest.fixture
def store():
    return McpTelemetryStore(Path(tempfile.mkdtemp()) / "audit.db")


@pytest.fixture
def chan():
    return AuditChannel("test_channel", "MENHIR_TEST_CHANNEL_AUDIT_ENABLED", corr_prefix="tc")


def test_default_off_is_noop(chan, store):
    chan.set_enabled(False)
    chan.audit("gate", "advisory", corr_id="tc-x", details={"reason": "slot"})
    assert chan.read("tc-x", store=store) == []


def test_off_emit_never_raises_with_unserializable(chan):
    chan.set_enabled(False)
    chan.audit("gate", "suppress", details={"obj": object()})  # OFF short-circuits; must not raise


def test_on_emit_never_raises_even_if_store_explodes(chan, monkeypatch, store):
    chan.set_enabled(True)
    monkeypatch.setattr(
        "menhir.infrastructure.audit_trail.telemetry_store", store, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(store, "record_lifecycle_event", _boom, raising=False)
    chan.audit("gate", "suppress", corr_id="tc-boom", details={"obj": object()})  # swallowed


def test_on_records_shape_and_replay_order(chan, store):
    chan.set_enabled(True)
    cid = "tc-order"
    for ev, st in [("intent", "current_state"), ("provenance", "fetched"),
                   ("gate", "advisory"), ("result", "no_suppression")]:
        record_lifecycle_event(component=chan.component, event=ev, state=st, episode_uuid=cid,
                               details={"k": 1}, store=store)
    rows = chan.read(cid, store=store)
    assert [(r["event"], r["state"]) for r in rows] == [
        ("intent", "current_state"), ("provenance", "fetched"),
        ("gate", "advisory"), ("result", "no_suppression"),
    ]
    assert all(r["component"] == "test_channel" for r in rows)


def test_begin_sets_ambient_id(chan):
    cid = chan.begin()
    assert cid.startswith("tc-") and chan.current_id() == cid
    assert chan.begin("tc-explicit") == "tc-explicit" and chan.current_id() == "tc-explicit"


def test_channels_are_independent():
    a = AuditChannel("chan_a", "ENV_A")
    b = AuditChannel("chan_b", "ENV_B")
    a.set_enabled(True)
    assert a.is_enabled() and not b.is_enabled()


def test_recall_channel_registered():
    assert RECALL.component == "recall_audit"
