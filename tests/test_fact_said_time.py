"""Fact statement time on recall facts (MENHIR_FRONTIER_FACT_SAID_TIME), offline.

valid_at is when a fact was true; it can predate when it was said ("for 3 months"). said_at is
the edge's reference_time, so the reader can tell which of two facts was said later (#168).
"""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.recall import TemporalFact
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.cypher import FACT_TEMPORAL_FIELDS
from menhir.mcp.formatters import _with_event_time
from menhir.services.context_builder import _source_time_lines
from menhir.services.recall_policies import _build_temporal_facts

ROW = {
    "node_uuid": "n1", "fact": "User has worn a Fitbit for 3 months",
    "valid_at": "2023-03-01T00:00:00Z", "invalid_at": None,
    "created_at": "2024-01-01T00:00:00Z", "expired_at": None,
    "said_at": "2023-06-01T10:00:00Z",
}


@pytest.mark.unit
def test_fact_query_reads_the_reference_time():
    assert "toString(r.reference_time) AS said_at" in FACT_TEMPORAL_FIELDS


@pytest.mark.unit
def test_said_at_is_set_only_with_the_flag():
    (off,) = _build_temporal_facts([ROW])["n1"]
    (on,) = _build_temporal_facts([ROW], said_time=True)["n1"]
    assert off.said_at is None
    assert on.said_at == "2023-06-01T10:00:00Z"


@pytest.mark.unit
def test_neo4j_zone_id_is_dropped():
    (fact,) = _build_temporal_facts(
        [dict(ROW, said_at="2023-11-30T20:25:00Z[UTC]")], said_time=True)["n1"]
    assert fact.said_at == "2023-11-30T20:25:00Z"


@pytest.mark.unit
def test_missing_reference_time_stays_none():
    (fact,) = _build_temporal_facts([dict(ROW, said_at=None)], said_time=True)["n1"]
    assert fact.said_at is None


def _scored(fact: TemporalFact):
    from types import SimpleNamespace
    return SimpleNamespace(temporal_facts=(fact,))


@pytest.mark.unit
def test_context_line_shows_said_time_only_when_present():
    (on,) = _build_temporal_facts([ROW], said_time=True)["n1"]
    (off,) = _build_temporal_facts([ROW])["n1"]
    assert _source_time_lines(_scored(on))[1] == (
        "  - 2023-03-01T00:00:00Z | said 2023-06-01T10:00:00Z | "
        "User has worn a Fitbit for 3 months | belief: current belief")
    assert _source_time_lines(_scored(off))[1] == (
        "  - 2023-03-01T00:00:00Z | User has worn a Fitbit for 3 months | belief: current belief")


@pytest.mark.unit
def test_api_item_carries_said_at_only_when_set():
    (on,) = _build_temporal_facts([ROW], said_time=True)["n1"]
    (off,) = _build_temporal_facts([ROW])["n1"]
    assert _with_event_time({}, on) == {"said_at": "2023-06-01T10:00:00Z"}
    assert _with_event_time({}, off) == {}


@pytest.mark.unit
def test_setting_default_and_env_reach_tuning(monkeypatch):
    # Shipped on; the engine-level tuning default stays off for callers that build it directly.
    assert MemorySettings().frontier_fact_said_time is True
    assert RetrievalTuningConfig().enable_fact_said_time is False
    monkeypatch.delenv("MENHIR_FRONTIER_FACT_SAID_TIME", raising=False)
    assert MemorySettings.from_env().retrieval_tuning().enable_fact_said_time is True
    monkeypatch.setenv("MENHIR_FRONTIER_FACT_SAID_TIME", "false")
    assert MemorySettings.from_env().retrieval_tuning().enable_fact_said_time is False
