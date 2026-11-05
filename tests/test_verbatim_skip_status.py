"""A verbatim-skipped client must report its projection's enrichment state, not its own READY.

Counterexample under test: the client is READY/verbatim_skipped while its evidence projection is
still PENDING/ENRICHING. A waiter that trusts the raw client row stops before anything was
extracted. Each counterexample has a companion "bites" test that swaps
``effective_processing_row`` for the identity function (the old behaviour) and asserts the
property is then violated, proving the test can fail.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from menhir.domain.models import ProcessingState
from menhir.domain.verbatim_skip_status import (
    PROJECTION_LOST_SUBSTAGE,
    SKIP_PROJECTION_FIELDS,
    effective_processing_row,
    is_verbatim_skipped,
)
from menhir.mcp import formatters
from menhir.mcp.tools.ops.get_enrichment_status import GetEnrichmentStatusTool
from menhir.services import ingest_intake
from menhir.services.enrichment_steps import still_owns_episode
from menhir.services.ingest_intake import IngestIntakeMixin

CLIENT = "123e4567-e89b-12d3-a456-426614174111"
PROJ = "123e4567-e89b-12d3-a456-426614174222"


def _projection(state, **kw):
    p = {f: None for f in SKIP_PROJECTION_FIELDS}
    p.update(uuid=PROJ, processing_state=state)
    p.update(kw)
    return p


def _client(projection, **kw):
    row = {
        "uuid": CLIENT,
        "processing_state": "READY",
        "processing_stage": "ready",
        "processing_substage": "verbatim_skipped",
        "processing_progress": 1.0,
        "processing_steps_total": 0,
        "processing_steps_completed": 0,
        "processing_owner": "worker-a",
        "processing_lease_expires_at": "2030-01-01T00:00:00Z",
        "processing_attempts": 2,
        "processing_completed_at": "client-done",
        "extraction_skipped_for": PROJ,
        "skip_projection": projection,
    }
    row.update(kw)
    return row


def _enriching(**kw):
    base = dict(
        processing_stage="extracting", processing_substage="llm", processing_progress=0.4,
        processing_steps_total=5, processing_steps_completed=2,
    )
    base.update(kw)
    return _projection("ENRICHING", **base)


# ---------------------------------------------------------------- unit: effective_processing_row

def test_none_stays_none():
    assert effective_processing_row(None) is None


@pytest.mark.parametrize("row", [
    {"uuid": "x", "processing_state": "PENDING", "processing_substage": "verbatim_skipped",
     "extraction_skipped_for": PROJ},
    {"uuid": "x", "processing_state": "ENRICHING", "processing_substage": "verbatim_skipped",
     "extraction_skipped_for": PROJ},
    {"uuid": "x", "processing_state": "FAILED", "processing_substage": "verbatim_skipped",
     "extraction_skipped_for": PROJ},
    {"uuid": "x", "processing_state": "READY", "processing_substage": "complete",
     "extraction_skipped_for": PROJ, "skip_projection": _enriching()},
    {"uuid": "x", "processing_state": "READY", "processing_substage": "verbatim_skipped",
     "extraction_skipped_for": None, "skip_projection": _enriching()},
    {"uuid": "x", "processing_state": "READY", "processing_substage": "verbatim_skipped",
     "extraction_skipped_for": "", "skip_projection": _enriching()},
    {"uuid": "x", "processing_state": "READY", "processing_substage": "verbatim_skipped",
     "skip_projection": _enriching()},
])
def test_non_skip_rows_are_unchanged(row):
    before = copy.deepcopy(row)
    out = effective_processing_row(row)
    assert out == before
    assert not is_verbatim_skipped(row)


@pytest.mark.parametrize("state", ["READY", ProcessingState.READY])
def test_state_string_and_enum_both_detected(state):
    row = _client(_enriching(), processing_state=state)
    assert is_verbatim_skipped(row)
    assert effective_processing_row(row)["processing_state"] == "ENRICHING"


def test_projection_ready_carries_projection_results():
    proj = _projection(
        "READY", resolved_episode_uuid="resolved-1", enriched_nodes_touched=3,
        enriched_edges_touched=4, processing_completed_at="proj-done")
    out = effective_processing_row(_client(proj))
    assert out["processing_state"] == "READY"
    assert out["processing_substage"] == "verbatim_skipped"
    assert out["resolved_episode_uuid"] == "resolved-1"
    assert out["enriched_nodes_touched"] == 3
    assert out["enriched_edges_touched"] == 4
    assert out["processing_completed_at"] == "proj-done"
    assert out["extraction_projection_uuid"] == PROJ


def test_projection_ready_keeps_clients_own_resolved_uuid():
    proj = _projection("READY", resolved_episode_uuid="from-projection")
    out = effective_processing_row(_client(proj, resolved_episode_uuid="client-own"))
    assert out["resolved_episode_uuid"] == "client-own"


def test_projection_in_flight_copies_progress_and_keeps_client_lease_fields():
    for state in ("PENDING", "ENRICHING"):
        proj = _enriching()
        proj["processing_state"] = state
        out = effective_processing_row(_client(proj))
        assert out["processing_state"] == state
        assert out["processing_stage"] == "extracting"
        assert out["processing_substage"] == "llm"
        assert out["processing_progress"] == 0.4
        assert out["processing_steps_total"] == 5
        assert out["processing_steps_completed"] == 2
        assert out["processing_completed_at"] is None
        # client's own owner/lease/attempts are not replaced by the projection's
        assert out["processing_owner"] == "worker-a"
        assert out["processing_lease_expires_at"] == "2030-01-01T00:00:00Z"
        assert out["processing_attempts"] == 2


@pytest.mark.parametrize("projection", [
    _projection("FAILED", processing_error="boom"),
    None,
])
def test_projection_failed_or_gone_reports_pending_projection_lost(projection):
    out = effective_processing_row(_client(projection, processing_error="stale"))
    assert out["processing_state"] == "PENDING"
    assert out["processing_stage"] == "queued"
    assert out["processing_substage"] == PROJECTION_LOST_SUBSTAGE
    assert out["processing_progress"] == 0.0
    assert out["processing_steps_completed"] == 0
    assert out["processing_error"] is None
    assert out["processing_completed_at"] is None


def test_missing_skip_projection_key_is_projection_lost():
    row = _client(None)
    del row["skip_projection"]
    assert effective_processing_row(row)["processing_substage"] == PROJECTION_LOST_SUBSTAGE


def test_input_is_not_mutated():
    for proj in (_enriching(), _projection("READY", resolved_episode_uuid="r"), _projection("FAILED"), None):
        row = _client(proj)
        before = copy.deepcopy(row)
        effective_processing_row(row)
        assert row == before


# ---------------------------------------------------------------- counterexample: MCP wait loop

class _SeqBackend:
    """Async backend returning the given rows in order; the last one repeats."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.polls = 0

    async def fetch_episode_processing(self, episode_uuid):
        self.polls += 1
        if len(self._rows) > 1:
            return self._rows.pop(0)
        return self._rows[0]

    async def get_queue_depth(self):
        return 1


def _ready_sequence():
    enriching = _client(_enriching())
    return [
        copy.deepcopy(enriching),
        copy.deepcopy(enriching),
        _client(_projection("READY", resolved_episode_uuid="resolved-9",
                            processing_completed_at="proj-done")),
    ]


def _run_collect(backend, **kw):
    return asyncio.run(formatters._collect_episode_status(
        backend, CLIENT, timeout_s=kw.get("timeout_s", 5), poll_interval_s=0.01))


def test_mcp_wait_does_not_stop_at_raw_ready_while_projection_enriching():
    backend = _SeqBackend(_ready_sequence())
    current, history, timed_out = _run_collect(backend)
    assert backend.polls == 3
    assert any(h["state"] == "ENRICHING" for h in history)
    assert current["processing_state"] == "READY"
    assert current["resolved_episode_uuid"] == "resolved-9"
    assert timed_out is False


def test_mcp_wait_times_out_enriching_when_projection_never_finishes():
    backend = _SeqBackend([_client(_enriching())])
    current, history, timed_out = _run_collect(backend, timeout_s=0.2)
    assert timed_out is True
    assert current["processing_state"] == "ENRICHING"
    assert backend.polls >= 2


def test_mcp_wait_counterexample_bites_without_fix(monkeypatch):
    """With the identity function (old behaviour) the wait returns after the first poll."""
    monkeypatch.setattr(formatters, "effective_processing_row", lambda row: row)
    backend = _SeqBackend(_ready_sequence())
    current, history, timed_out = _run_collect(backend)
    assert backend.polls == 1
    assert current["processing_state"] == "READY"
    assert current.get("resolved_episode_uuid") is None
    assert not any(h["state"] == "ENRICHING" for h in history)


# ---------------------------------------------------------------- counterexample: ingest wait

class _SyncAdapter:
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def fetch_episode_processing(self, episode_uuid):
        self.calls += 1
        if len(self._rows) > 1:
            return self._rows.pop(0)
        return self._rows[0]


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _intake_wait(adapter, timeout_s=5.0):
    return asyncio.run(_Intake(adapter).wait_for_episode_processing(
        CLIENT, timeout_s=timeout_s, poll_interval_s=0.01))


def test_ingest_wait_returns_only_once_projection_is_ready():
    adapter = _SyncAdapter(_ready_sequence())
    row = _intake_wait(adapter)
    assert adapter.calls == 3
    assert row["processing_state"] == "READY"
    assert row["resolved_episode_uuid"] == "resolved-9"


def test_ingest_wait_projection_failed_keeps_polling_and_returns_projection_lost():
    adapter = _SyncAdapter([_client(_projection("FAILED", processing_error="boom"))])
    row = _intake_wait(adapter, timeout_s=0.2)
    assert adapter.calls >= 3  # PENDING is not terminal: polled to the deadline plus final fetch
    assert row["processing_state"] == "PENDING"
    assert row["processing_substage"] == PROJECTION_LOST_SUBSTAGE


def test_ingest_wait_final_fetch_is_also_effective():
    adapter = _SyncAdapter([_client(_enriching())])
    row = _intake_wait(adapter, timeout_s=0.1)
    assert row["processing_state"] == "ENRICHING"


def test_ingest_wait_counterexample_bites_without_fix(monkeypatch):
    monkeypatch.setattr(ingest_intake, "effective_processing_row", lambda row: row)
    adapter = _SyncAdapter(_ready_sequence())
    row = _intake_wait(adapter)
    assert adapter.calls == 1
    assert row.get("resolved_episode_uuid") is None  # returned before the projection finished


# ---------------------------------------------------------------- get_enrichment_status no-wait

def _status_text(row, monkeypatch=None):
    backend = SimpleNamespace(
        fetch_episode_processing=_async_const(row),
        get_queue_depth=_async_const(0),
    )
    tool = GetEnrichmentStatusTool()
    tool.get_backend = lambda: backend
    return asyncio.run(tool.endpoint(CLIENT, wait=False))


def _async_const(value):
    async def _f(*a, **k):
        return value
    return _f


def test_get_enrichment_status_nowait_reports_projection_pending_not_ready():
    proj = _projection("PENDING", processing_stage="queued", processing_substage="waiting")
    text = _status_text(_client(proj))
    assert "status: PENDING" in text
    assert "status: READY" not in text
    assert "substage: waiting" in text


def test_get_enrichment_status_nowait_ready_projection_reports_resolved_uuid():
    proj = _projection("READY", resolved_episode_uuid="resolved-7")
    text = _status_text(_client(proj))
    assert "status: READY" in text
    assert "enriched_episode_uuid: resolved-7" in text


def test_get_enrichment_status_nowait_counterexample_bites_without_fix(monkeypatch):
    from menhir.mcp.tools.ops import get_enrichment_status as mod
    monkeypatch.setattr(mod, "effective_processing_row", lambda row: row)
    text = _status_text(_client(_projection("PENDING")))
    assert "status: READY" in text  # the old, wrong answer


# ---------------------------------------------------------------- work fence reads the raw row

def test_still_owns_episode_ignores_projection_state_of_a_skipped_client():
    """The client row is READY; a work fence must not see the projection's ENRICHING."""
    row = _client(_enriching(), processing_owner="worker-a")

    adapter = SimpleNamespace(fetch_episode_processing=lambda uuid: row)
    assert still_owns_episode(adapter, CLIENT, "worker-a") is False

    # sanity: the same helper does answer True for a genuinely ENRICHING claimed row
    live = dict(row, processing_state="ENRICHING", processing_substage="llm")
    adapter_live = SimpleNamespace(fetch_episode_processing=lambda uuid: live)
    assert still_owns_episode(adapter_live, CLIENT, "worker-a") is True
