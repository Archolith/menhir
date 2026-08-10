"""Formatter tests for temporal facts (Rung 1A serialization fix + Rung 1C rendering).

The MCP recall path serializes RecallResult via asdict() BEFORE the formatter runs,
so temporal facts arrive as plain dicts (not TemporalFact dataclasses). These tests
guard that the formatter tolerates both shapes and renders the happened-vs-learned
`when` phrase.
"""

from __future__ import annotations

from types import SimpleNamespace

from menhir.domain.recall import TemporalFact
from menhir.mcp.formatters import _compact_scored_item, _format_when


def _scored_with_facts(facts: object) -> SimpleNamespace:
    """Build a minimal scored-item stand-in with the given temporal_facts payload."""
    return SimpleNamespace(
        uuid="u1",
        name="Alice",
        scope="PERSISTENT",
        final_score=0.9,
        memory_type="SEMANTIC",
        summary="summary",
        content="content",
        breakdown={"semantic_similarity": 0.8},
        temporal_facts=facts,
    )


class TestTemporalFactsSerialization:
    def test_dict_path_does_not_raise(self) -> None:
        """Post-asdict dict facts (the MCP path) must serialize without AttributeError."""
        fact_dict = {
            "fact": "lives_in Munich",
            "valid_at": "2025-03-01T00:00:00Z",
            "invalid_at": None,
            "created_at": "2025-03-02T00:00:00Z",
            "expired_at": None,
            "is_current_belief": True,
            "temporal_role": "current_belief",
        }
        item = _compact_scored_item(_scored_with_facts([fact_dict]), compact=False)
        assert "temporal_facts" in item
        tf = item["temporal_facts"][0]
        assert tf["fact"] == "lives_in Munich"
        assert tf["is_current_belief"] is True
        assert tf["temporal_role"] == "current_belief"
        assert "when" in tf

    def test_object_path(self) -> None:
        """Direct TemporalFact dataclass facts must serialize the same way."""
        tf_obj = TemporalFact(
            fact="lives_in Berlin",
            valid_at="2024-01-01T00:00:00Z",
            invalid_at="2025-02-28T00:00:00Z",
            created_at="2024-01-02T00:00:00Z",
            expired_at="2025-03-02T00:00:00Z",
            is_current_belief=False,
            temporal_role="superseded_belief",
        )
        item = _compact_scored_item(_scored_with_facts((tf_obj,)), compact=False)
        tf = item["temporal_facts"][0]
        assert tf["fact"] == "lives_in Berlin"
        assert tf["is_current_belief"] is False

    def test_empty_facts_omits_key(self) -> None:
        """No facts -> no temporal_facts key (back-compat with existing output)."""
        item = _compact_scored_item(_scored_with_facts(()), compact=False)
        assert "temporal_facts" not in item

    def test_compact_mode_retains_source_time_facts(self) -> None:
        """Compact mode keeps source time because it can change answer selection."""
        item = _compact_scored_item(
            _scored_with_facts([{
                "fact": "French press ratio uses 5 oz of water",
                "valid_at": "2023-06-30T11:33:00Z",
            }]),
            compact=True,
        )
        assert item["temporal_facts"][0]["valid_at"] == "2023-06-30T11:33:00Z"


class TestFormatWhen:
    def test_current_open_interval(self) -> None:
        when = _format_when(
            {"valid_at": "2025-03-01", "invalid_at": None,
             "created_at": "2025-03-02", "expired_at": None}
        )
        assert "happened from 2025-03-01" in when
        assert "learned 2025-03-02" in when
        assert "superseded" not in when

    def test_superseded_closed_interval(self) -> None:
        when = _format_when(
            {"valid_at": "2024-01-01", "invalid_at": "2025-02-28",
             "created_at": "2024-01-02", "expired_at": "2025-03-02"}
        )
        assert "until 2025-02-28" in when
        assert "superseded 2025-03-02" in when

    def test_unstated_times(self) -> None:
        when = _format_when(
            {"valid_at": None, "invalid_at": None, "created_at": None, "expired_at": None}
        )
        assert "time unstated" in when
