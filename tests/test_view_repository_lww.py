"""LWW ordering guard (fold-algebra Law 1): a temporally-older event must not overwrite a
current value register. Covers the deterministic core — the `valid_at` comparison and the
per-kind `lww_register` flag — so a refactor can't silently revert the reconcile to
arrival-ordered. (The full skip-in-graph behavior is exercised by the live integration check;
these unit tests guard the parsing/comparison edge cases that logic depends on.)"""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_repository import (
    AdmissionAuditKind,
    CounterKind,
    TimelineKind,
    ViewKind,
    ViewRepository,
    _is_older,
    _parse_dt,
)


@pytest.mark.unit
def test_lww_register_flag_is_kind_scoped():
    # counter is a value register -> newer valid_at wins; timeline is set/list -> sig-driven.
    assert CounterKind.lww_register is True
    assert TimelineKind.lww_register is False
    assert ViewKind.lww_register is False  # safe default: guard off unless a kind opts in


@pytest.mark.unit
def test_is_older_strict_ordering():
    older = "2026-01-01T00:00:00+00:00"
    newer = "2026-06-01T00:00:00+00:00"
    assert _is_older(older, newer) is True     # candidate strictly earlier -> stale
    assert _is_older(newer, older) is False     # candidate later -> not stale
    assert _is_older(newer, newer) is False     # equal -> NOT older (equal proceeds to supersede)


@pytest.mark.unit
def test_is_older_fails_open_on_bad_input():
    # unparseable or missing timestamps must NOT trigger a skip (fall back to pre-guard behavior).
    good = "2026-06-01T00:00:00Z"
    assert _is_older("not-a-date", good) is False
    assert _is_older(None, good) is False
    assert _is_older(good, None) is False


@pytest.mark.unit
def test_parse_dt_handles_zulu_and_neo4j_nanoseconds():
    # neo4j toString(datetime) emits 'Z' and 9-digit fractional seconds, which
    # datetime.fromisoformat rejects; the guard's comparison must still work.
    assert _parse_dt("2026-01-04T00:00:00Z") is not None
    assert _parse_dt("2026-01-04T00:00:00.000000000Z") is not None
    assert _parse_dt("2026-01-04T12:34:56.123456789+00:00") is not None
    assert _parse_dt("") is None
    assert _parse_dt(None) is None


@pytest.mark.unit
def test_parse_dt_strips_neo4j_zone_id_suffix():
    # Regression: toString(n.valid_at) read back from neo4j carries a trailing zone-id suffix
    # ("2026-07-21T19:13:04.572Z[UTC]"). Without stripping it, _parse_dt returned None, _is_older
    # fell open, and the LWW guard silently never fired for the counter/metric registers. Same
    # defect fixed in scalar_state_fold (bc970a2b) and scalar_view_authority.
    assert _parse_dt("2026-07-21T19:13:04.572Z[UTC]") is not None
    assert _parse_dt("2026-07-21T19:13:04.572+00:00[Europe/London]") is not None
    older = "2026-07-21T19:10:51.478Z[UTC]"
    newer = "2026-07-21T19:13:04.572Z[UTC]"
    assert _is_older(older, newer) is True      # the guard must SEE the stale write
    assert _is_older(newer, older) is False
    # mixed zoned/plain forms compare correctly too (the real read path mixes them)
    assert _is_older(older, "2026-07-21T19:13:04.572Z") is True


@pytest.mark.unit
def test_is_older_normalizes_naive_vs_aware():
    # a naive date (e.g. a timeline entry '2026-01-01') compared to an aware neo4j timestamp
    # must not raise; naive is treated as UTC.
    assert _is_older("2026-01-01", "2026-06-01T00:00:00Z") is True
    assert _is_older("2026-09-01", "2026-06-01T00:00:00Z") is False


@pytest.mark.unit
def test_admission_audit_kind_registered():
    # The admission gate's audit trail (menhir/domain/truth/admission_gate.py) must ride on
    # the existing View storage, not a new schema/table — this guards the registration so a
    # refactor can't silently drop it from ViewRepository.KINDS.
    assert "admission_audit" in ViewRepository.KINDS
    assert isinstance(ViewRepository.KINDS["admission_audit"], AdmissionAuditKind)
    assert AdmissionAuditKind.lww_register is False  # every verdict is its own event, not a register


@pytest.mark.unit
def test_admission_audit_kind_write_props_roundtrip():
    kind = AdmissionAuditKind()
    payload = {
        "requested_source": "user",
        "effective_source": "agent_inference",
        "granted": False,
        "turn_evidence_uuid": "turn-123",
        "reason": "no turn_evidence_uuid provided",
    }
    key = kind.key_discriminator(payload)
    assert key == "admission_audit"

    props = kind.write_props("session-1", key, payload)
    assert props["view_value"] == 0.0  # granted=False -> 0.0
    assert props["requested_source"] == "user"
    assert props["effective_source"] == "agent_inference"
    assert props["turn_evidence_uuid"] == "turn-123"

    name, summary = kind.surface("session-1", payload)
    assert "denied" in name
    assert "session-1" in name
    assert "agent_inference" in summary

    # Round-trip through parse() as if read back from Neo4j (read_fields shape).
    row = {
        "uuid": "n-1", "subject": "session-1", "granted": False,
        "requested_source": "user", "effective_source": "agent_inference",
        "reason": "no turn_evidence_uuid provided", "turn_evidence_uuid": "turn-123",
        "valid_at": "2026-07-12T00:00:00+00:00",
    }
    parsed = kind.parse(row)
    assert parsed["granted"] is False
    assert parsed["turn_evidence_uuid"] == "turn-123"


@pytest.mark.unit
def test_admission_audit_kind_signature_distinguishes_verdicts():
    # Distinct verdicts (e.g. a denial vs. a later grant for the same subject) must produce
    # distinct signatures so the write core creates a new row instead of no-op refreshing --
    # each admission attempt is its own auditable event, not a value register.
    kind = AdmissionAuditKind()
    denied = {
        "requested_source": "user", "effective_source": "agent_inference",
        "granted": False, "turn_evidence_uuid": None, "reason": "no turn_evidence_uuid provided",
    }
    granted = {
        "requested_source": "user", "effective_source": "user",
        "granted": True, "turn_evidence_uuid": "turn-123", "reason": "grounded",
    }
    assert kind.signature(denied) != kind.signature(granted)
